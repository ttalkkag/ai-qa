import hashlib
import json
import os
import stat
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.errors import DomainError
from .models import CheckResult, Run
from .processes import launch_process
from .services import TERMINAL, transition_run
from .source_guard import repository_fingerprint
from .verification import assess_check
from .workspace import WorkspaceError, collect_tree, commit_candidate, create_workspace, freeze_candidate, source_tree_sha256


def _alive(job):
    run = Run.objects.get(pk=job.subject_id)
    if run.status == "CANCEL_REQUESTED":
        raise DomainError("USER_CANCELLED")
    if run.status in TERMINAL:
        raise DomainError("STALE_WORKER")
    if run.lease_epoch != job.lease_epoch or run.lease_owner != job.lease_owner:
        raise DomainError("STALE_WORKER")
    return run


def _copy_dependencies(source, policy):
    from apps.projects.models import DependencyBundle
    from apps.projects.services import verify_dependency_bundle
    from integrations.codex.container import runtime_info

    copied = set()
    runtime = None
    for command in policy.get("preparation_commands", []):
        bundle_id = command.get("dependency_bundle_id")
        if not bundle_id or str(bundle_id) in copied:
            continue
        bundle = verify_dependency_bundle(DependencyBundle.objects.get(pk=bundle_id))
        runtime = runtime or runtime_info()
        system = runtime["system"]
        if bundle.platform not in {system, system.lower(), f"{system.lower()}-{runtime['machine']}"}:
            raise DomainError("DEPENDENCY_MISSING", "등록된 의존성 자료의 플랫폼이 Docker 실행 이미지와 다릅니다.")
        if bundle.runtime != runtime["runtime"]:
            raise DomainError("DEPENDENCY_MISSING", "등록된 의존성 자료의 Python runtime이 현재 준비 도구와 다릅니다.")
        source_entries = collect_tree(source)
        if not any(hashlib.sha256(entry.content).hexdigest() == bundle.lock_file_hash for entry in source_entries.values()):
            raise DomainError("DEPENDENCY_MISSING", "후보 코드의 lock에 맞는 의존성 자료가 필요합니다.")
        target = source / ".aiqa-inputs" / str(bundle.pk)
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
        registered = Path(bundle.registered_path)
        for entry in bundle.manifest:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise DomainError("DEPENDENCY_CHANGED")
            item = registered / relative
            current = item
            while current != registered:
                if current.is_symlink():
                    raise DomainError("DEPENDENCY_CHANGED")
                current = current.parent
            descriptor = os.open(item, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry["size"]:
                    raise DomainError("DEPENDENCY_CHANGED")
                content = stream.read(entry["size"] + 1)
            if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise DomainError("DEPENDENCY_CHANGED")
            destination = target / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(content)
            destination.chmod(0o400)
        copied.add(str(bundle_id))


def _prepare(job, source, policy):
    from apps.ai.services import execute_check

    expected_hash = source_tree_sha256(source, policy)
    _copy_dependencies(source, policy)
    for command in policy.get("preparation_commands", []):
        _alive(job)
        command = {**command, "generated_paths": [".venv", ".aiqa-output"], "config_revision": policy["config_revision"]}
        outcome = execute_check(job, source, command, launch_process)
        if source_tree_sha256(source, policy) != expected_hash:
            raise DomainError("SOURCE_CHANGED_DURING_CHECK")
        if outcome.returncode or outcome.timed_out or not outcome.output_complete:
            raise DomainError("DEPENDENCY_MISSING", "등록된 오프라인 환경 준비에 실패했습니다.")


def _check_phase(job, source, phase, policy, expected_hash):
    from apps.ai.services import execute_check

    results = []
    for command in policy["verification_commands"]:
        run = _alive(job)
        if source_tree_sha256(source, policy) != expected_hash:
            raise DomainError("SOURCE_CHANGED_DURING_CHECK")
        report = source / command["report_path"] if command.get("report_path") else None
        if report:
            if report.exists() or report.is_symlink():
                raise DomainError("CHECK_REPORT_NOT_FRESH")
            report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        started = timezone.now()
        Run.objects.filter(pk=run.pk, lease_epoch=job.lease_epoch).update(current_stage=f"{phase}:{command['command_id']}",
                                                                      deadline_at=started + timedelta(seconds=command.get("timeout_sec", 300)))
        output = execute_check(job, source, {**command, "config_revision": policy["config_revision"]}, launch_process)
        ended = timezone.now()
        if source_tree_sha256(source, policy) != expected_hash:
            raise DomainError("SOURCE_CHANGED_DURING_CHECK")
        outcome = assess_check(command, output.returncode, output.stdout, output_complete=output.output_complete,
                               timed_out=output.timed_out, report_path=report, report_started_at=started)
        row = CheckResult.objects.create(
            run=run, phase=phase, check_id=command["command_id"], config_version=run.project_config_version,
            argv=command["argv"], cwd=command.get("cwd", "."), required=command.get("required", True),
            started_at=started, finished_at=ended, duration=(ended - started).total_seconds(),
            checked_source_tree_sha256=expected_hash,
            exit_code=outcome["exit_code"], outcome=outcome["outcome"], parser_id=outcome.get("parser_id") or "",
            parser_version=outcome.get("parser_version"), report_path=command.get("report_path", ""),
            report_sha256=outcome.get("report_sha256") or "", collected_count=outcome.get("collected_count"),
            passed_count=outcome.get("passed_count"), failed_count=outcome.get("failed_count"), skipped_count=outcome.get("skipped_count"),
            output_complete=outcome["output_complete"], reason_code=outcome.get("reason_code", ""),
        )
        results.append(row)
        if phase == "BASELINE" and row.outcome not in {"PASS", "FAIL"}:
            raise DomainError("BASELINE_UNVERIFIED")
    return results


def _modify(job, workspace, run, policy):
    from apps.ai.models import AIInvocation
    from apps.ai.prompts import PROMPT_VERSION, implementation_prompt
    from apps.ai.services import invoke, make_scope

    prompt = implementation_prompt(run.request_revision.snapshot)
    if len(prompt) > 100000:
        raise DomainError("CONTEXT_TOO_LARGE")
    deadline = timezone.now() + timedelta(minutes=30)
    temporary = workspace / ".aiqa-output" / "tmp"
    temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    invocation = AIInvocation.objects.create(
        actor=run.requested_by, run=run, profile_id=run.profile_snapshot["id"], purpose="implementation",
        profile_snapshot=run.profile_snapshot, prompt=prompt, prompt_version=PROMPT_VERSION, state="RUNNING", started_at=timezone.now(),
        requested_model=run.profile_snapshot["model_id"], requested_effort=run.profile_snapshot["effort"],
        context_manifest={"revision_id": str(run.request_revision_id), "revision_hash": run.request_revision.content_hash,
                          "base_commit_sha": run.baseline_commit_sha, "project_config_version": run.project_config_version},
        context_manifest_hash=hashlib.sha256(prompt.encode()).hexdigest(),
    )
    Run.objects.filter(pk=run.pk, lease_epoch=job.lease_epoch).update(deadline_at=deadline)
    try:
        result = invoke(prompt, run.profile_snapshot, make_scope(workspace, writable=True, write_paths=policy["write_paths"], source_read_paths=policy["read_paths"], temporary_dir=temporary),
                        deadline, job=job, process_launcher=launch_process, attachments=run.request_revision.attachment_snapshot)
    except DomainError as exc:
        unconfirmed = job.processes.filter(ended_at__isnull=True).exists()
        invocation.state = "RUNNING" if unconfirmed else "FAILED"
        invocation.error_code = exc.code
        invocation.ended_at = None if unconfirmed else timezone.now()
        invocation.save(update_fields=["state", "error_code", "ended_at"])
        raise
    invocation.state = "FAILED" if result.error_code else "SUCCEEDED"
    invocation.error_code = result.error_code
    invocation.exit_code = result.exit_code
    invocation.final_response = result.final_response
    invocation.effective_model = result.effective_model
    invocation.effective_effort = result.effective_effort
    invocation.reported_tokens = result.reported_tokens
    invocation.execution_events = result.execution_events
    invocation.trace_complete = result.trace_complete
    invocation.ended_at = timezone.now()
    invocation.save()
    if result.error_code:
        raise DomainError(result.error_code)
    return result


def execute_run(job):
    from apps.accounts.services import require_access
    from apps.ai.services import require_sandbox_ready
    from apps.artifacts.services import seal_result
    from apps.ai.evaluation import result_evaluation
    from apps.projects.services import resolve_execution_input

    run = _alive(job)
    require_access(job.actor, "request.submit", project_id=run.project_id, obj=run.request_revision.request)
    execution = resolve_execution_input(job.actor, run.project_id, run.project_config_version, run.baseline_commit_sha)
    policy = {**execution.policy, "config_revision": execution.config_revision}
    if not any(command.get("required", True) for command in policy.get("verification_commands", [])):
        raise DomainError("PROJECT_NOT_READY")
    require_sandbox_ready(run.project)
    try:
        original = repository_fingerprint(execution.config_revision.canonical_repo_path)
        workspace = create_workspace(execution.config_revision.canonical_repo_path, run.baseline_commit_sha, run.pk, policy, settings.AIQA_DATA_DIR)
        Run.objects.filter(pk=run.pk, lease_epoch=job.lease_epoch).update(workspace_id=str(run.pk), snapshot_base_commit_sha=workspace.snapshot_base_commit_sha)
        _prepare(job, workspace.baseline_checks, policy)
        _check_phase(job, workspace.baseline_checks, "BASELINE", policy, workspace.baseline_tree_sha256)
        _prepare(job, workspace.workspace, policy)
        _alive(job)
        run = transition_run(run.pk, "RUNNING", lease_epoch=job.lease_epoch)
        modification = _modify(job, workspace.workspace, run, policy)
        _alive(job)
        run = transition_run(run.pk, "VERIFYING", lease_epoch=job.lease_epoch)
        frozen = freeze_candidate(workspace, policy)
        _prepare(job, frozen.after_checks, policy)
        after = _check_phase(job, frozen.after_checks, "AFTER", policy, frozen.tree_sha256)
        _alive(job)
        if any(check.required and check.outcome != "PASS" for check in after):
            raise DomainError("CHECKS_FAILED")
        if run.processes.filter(ended_at__isnull=True).exists():
            raise DomainError("PROCESS_TERMINATION_UNCONFIRMED")
        if source_tree_sha256(frozen.after_checks, policy) != frozen.tree_sha256:
            raise DomainError("SOURCE_CHANGED_DURING_CHECK")
        if repository_fingerprint(execution.config_revision.canonical_repo_path) != original:
            raise DomainError("ORIGINAL_CHANGED", "실행 중 원본의 변경이 감지되었습니다. 변경을 되돌리지 않았으며 별도 확인이 필요합니다.")
        checks = list(run.checks.values())
        for check in checks:
            check.pop("id", None)
            check.pop("run_id", None)
            for field in ("started_at", "finished_at"):
                check[field] = check[field].isoformat()
            if check.get("log_artifact_id"):
                check["log_artifact_id"] = str(check["log_artifact_id"])
        evidence = {
            "baseline_dir": str(workspace.full_baseline), "candidate_dir": str(frozen.path),
            "baseline_commit_sha": run.baseline_commit_sha, "project_config_version": run.project_config_version,
            "request_revision_id": str(run.request_revision_id), "checked_source_tree_sha256": frozen.tree_sha256,
            "processes_stopped": True, "checks": checks, "complete": True, "visibility": "NORMAL",
            "original_repository_check": original,
            "evaluation": result_evaluation(run.request_revision.snapshot, checks,
                getattr(modification, "execution_events", []), getattr(modification, "trace_complete", False)),
            "summary": f"AI 작업 보고\n{getattr(modification, 'final_response', '') or '별도 작업 보고가 없습니다'}\n\n앱 검증 결과\n변경 파일 {len(frozen.changed_paths)}개 · 등록된 필수 AFTER 검사 통과\n원본 저장소는 유지되었으며 원격 발행은 하지 않았습니다\n요구사항 충족 여부는 위 보고와 변경 내용을 함께 확인해 주세요",
        }
        bundle = seal_result(run.pk, evidence)
        if bundle.completeness != "COMPLETE" or bundle.visibility != "NORMAL":
            transition_run(run.pk, "FAILED", lease_epoch=job.lease_epoch, result_bundle_id=bundle.pk,
                           error_code="RESULT_RESTRICTED" if bundle.visibility != "NORMAL" else "RESULT_INCOMPLETE",
                           user_message="결과의 완전성 또는 비밀 검사를 통과하지 못했습니다.")
            return
        local_git = commit_candidate(workspace, frozen, policy)
        Run.objects.filter(pk=run.pk, lease_epoch=job.lease_epoch).update(
            result_branch=local_git["branch"], result_commit_sha=local_git["commit"])
        transition_run(run.pk, "SUCCEEDED", lease_epoch=job.lease_epoch, result_bundle_id=bundle.pk, message="검증된 로컬 결과를 저장했습니다.")
    except WorkspaceError as exc:
        raise DomainError(exc.code, str(exc)) from None
