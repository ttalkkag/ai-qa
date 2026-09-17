import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from apps.ai.evaluation import compare_reports, dataset_hash, grade_case
from apps.ai import evaluation
from apps.ai.prompts import PROMPT_VERSION, implementation_prompt
from apps.ai.services import make_scope
from apps.errors import DomainError
from apps.runs.source_guard import repository_fingerprint
from apps.runs import workspace as workspace_module
from apps.runs.workspace import _git, commit_candidate, create_workspace, freeze_candidate, WorkspaceError
from integrations.codex import adapter, container


def run_container(scope, command, image, *, prompt=None, timeout=180):
    name = "aiqa-" + uuid4().hex
    argv = container.run_argv(name, scope, command, image=image,
                              auth_file=container.login_file() if prompt is not None else None)
    try:
        return subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
    finally:
        container.remove(name)


class Command(BaseCommand):
    help = "합성 프로젝트에서 실제 로컬 Codex 행동 평가 (원본 DB·저장소·원격 변경 없음)"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true", help="실제 Codex 이용량 발생에 동의하고 실행")
        parser.add_argument("--model")
        parser.add_argument("--effort")
        parser.add_argument("--variant", choices=["current", "minimal"], default="current")
        parser.add_argument("--repeats", type=int, default=1)
        parser.add_argument("--case", action="append", dest="cases")
        parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"))

    def handle(self, *args, **options):
        if options["compare"]:
            try:
                reports = [json.loads(Path(path).read_text()) for path in options["compare"]]
                self.stdout.write(json.dumps(compare_reports(*reports), ensure_ascii=False, indent=2))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise CommandError(str(exc)) from None
            return
        if not options["live"]:
            raise CommandError("실제 모델 평가에는 --live가 필요합니다. 일반 make check는 AI를 호출하지 않습니다")
        if not 1 <= options["repeats"] <= 5:
            raise CommandError("--repeats는 1~5로 지정하세요")
        try:
            adapter.validate_model_values(options["model"], options["effort"])
            image = container.ready()
            container.login_file()
        except DomainError as exc:
            raise CommandError(exc.code) from None
        fixture = Path(settings.BASE_DIR) / "tests/fixtures/agent_evals"
        cases = json.loads((fixture / "cases.json").read_text())
        if options["cases"]:
            if set(options["cases"]) - {case["id"] for case in cases}:
                raise CommandError("알 수 없는 평가 사례입니다")
            cases = [case for case in cases if case["id"] in options["cases"]]
        output = Path(settings.AIQA_DATA_DIR).resolve() / "evaluations" / str(uuid4())
        output.mkdir(parents=True, mode=0o700)
        repo = output / "fixture"
        shutil.copytree(fixture / "source", repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _git(repo, "init", "-q", "--template=", "--initial-branch=main")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "test: initialize agent evaluation fixture")
        sha = _git(repo, "rev-parse", "HEAD").decode().strip()
        before = repository_fingerprint(repo)
        profile = {"model_id": options["model"], "effort": options["effort"]}
        report = {"schema_version": 1, "dataset_hash": dataset_hash(cases), "fixture_sha": sha,
                  "model": options["model"], "effort": options["effort"], "image_id": image,
                  "variant": options["variant"], "prompt_version": PROMPT_VERSION,
                  "prompt_template_hash": hashlib.sha256(implementation_prompt({}, variant=options["variant"]).encode()).hexdigest(),
                  "grader_hash": hashlib.sha256(Path(evaluation.__file__).read_bytes() + Path(__file__).read_bytes()
                                                + Path(workspace_module.__file__).read_bytes()).hexdigest(),
                  "repeats": options["repeats"], "case_ids": [case["id"] for case in cases], "results": []}
        report_path = output / "report.json"
        self.stdout.write(f"평가 결과: {report_path}")
        # No production database or source repository is used by this command.
        with override_settings(AIQA_DATA_DIR=output / "runtime"):
            for case in cases:
                for repeat in range(1, options["repeats"] + 1):
                    row = self.evaluate(case, repeat, repo, sha, profile, image, options["variant"])
                    if repository_fingerprint(repo) != before:
                        raise CommandError("합성 원본 변경이 감지되었습니다")
                    report["results"].append(row)
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    self.stdout.write(f"{case['id']} #{repeat}: {row['grade']['deterministic']} / 정성 평가 NEEDS_REVIEW")
        if any(row["grade"]["deterministic"] != "PASS" for row in report["results"]):
            raise CommandError(f"평가 실패 사례가 있습니다: {report_path}")

    def evaluate(self, case, repeat, repo, sha, profile, image, variant):
        started = time.monotonic()
        policy = {"read_paths": ["."], "write_paths": ["."]}
        workspace = create_workspace(repo, sha, uuid4(), policy, settings.AIQA_DATA_DIR)
        temporary = workspace.workspace / ".aiqa-output" / "tmp"
        temporary.mkdir(parents=True)
        scope = make_scope(workspace.workspace, writable=True, write_paths=["."], temporary_dir=temporary)
        prompt = implementation_prompt({"goal": case["prompt"], "acceptance_criteria": [case["rubric"]]}, variant=variant)
        local_git = {}
        checks = {}
        changed = []
        result = adapter.InvocationResult(error_code="NOT_STARTED")
        stage_error = ""
        try:
            process = run_container(scope, container.codex_command(profile, workspace.workspace), image, prompt=prompt)
            result = adapter.parse_jsonl(process.stdout, returncode=process.returncode,
                                        requested_model=profile["model_id"], requested_effort=profile["effort"])
            frozen = freeze_candidate(workspace, policy)
            changed = list(frozen.changed_paths)
            check_scope = make_scope(frozen.after_checks)
            for name, assertion in case["checks"].items():
                script = "import sys; sys.path.insert(0, '.'); import app, auth; " + assertion
                check = run_container(check_scope, ["python3", "-I", "-c", script], image, timeout=30)
                checks[name] = check.returncode
            grade = grade_case(case, changed_paths=changed, checks=checks, result=result)
            if grade["deterministic"] == "PASS":
                local_git = commit_candidate(workspace, frozen, policy)
        except (DomainError, WorkspaceError, subprocess.SubprocessError, OSError) as exc:
            stage_error = getattr(exc, "code", type(exc).__name__)
            grade = grade_case(case, changed_paths=changed, checks=checks, result=result)
            grade["assertions"]["pipeline"] = False
            grade["deterministic"] = "FAIL"
        return {"id": case["id"], "repeat": repeat, "grade": grade, "changed_paths": changed, "checks": checks,
                "error_code": stage_error or result.error_code, "tokens": result.reported_tokens,
                "effective_model": result.effective_model, "effective_effort": result.effective_effort,
                "duration_seconds": round(time.monotonic() - started, 3),
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(), "final_response": result.final_response,
                "execution_events": result.execution_events, "trace_complete": result.trace_complete,
                "local_git": local_git, "workspace": str(workspace.workspace)}
