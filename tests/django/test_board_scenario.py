import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from apps.accounts.models import User
from apps.accounts import services as accounts
from apps.ai.models import AIInvocation, SandboxCheck
from apps.artifacts.models import ResultBundle
from apps.conversations.services import start_conversation
from apps.errors import DomainError
from apps.projects import services as projects
from apps.runs import services as runs, worker
from apps.runs.models import CheckResult, Job, ProcessRecord, Run
from apps.runs.verification import assess_check
from apps.work_requests import services as requests
from apps.work_requests.models import RequestSubmission


pytestmark = pytest.mark.django_db


@pytest.fixture
def board_scenario(tmp_path, settings):
    root = tmp_path.resolve()
    runtime = root / "runtime"
    runtime.mkdir()
    settings.AIQA_DATA_DIR = runtime
    settings.AIQA_CODEX_BIN = str(root / "codex-not-installed")
    source = root / "board"
    fixture = Path(__file__).parents[1] / "fixtures" / "board"
    shutil.copytree(fixture, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": str(root / "empty-home"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "",
    }

    def git(*args):
        return subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(source), *args],
            env=environment, capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "user.name=Board Fixture", "-c", "user.email=board@localhost", "commit", "-qm", "Board baseline")
    baseline_sha = git("rev-parse", "HEAD")
    original = {
        path: hashlib.sha256((source / path).read_bytes()).hexdigest()
        for path in git("ls-files", "-z").split("\0") if path
    }
    admin = User.objects.create_user(
        "board-admin", password="Board-host-synthetic-4926!", display_name="시험 관리자", role="ADMIN",
    )
    manager = accounts.create_account(
        admin, "board-manager", "게시판 담당자", "MANAGER", "Board-initial-synthetic-4926!",
    )
    manager = accounts.change_password(manager, "Board-initial-synthetic-4926!", "Board-manager-synthetic-8739!")
    policy = {
        "read_paths": ["."], "write_paths": ["board.py"],
        "verification_commands": [{
            "command_id": "board-baseline", "display_name": "게시판 기존 동작",
            "kind": "TEST", "parser_id": "junit-xml", "parser_version": 1,
            "report_path": ".aiqa-output/board-baseline/report.xml",
            "argv": [sys.executable, "-I", "baseline_checks.py", "--junit", ".aiqa-output/board-baseline/report.xml"],
            "cwd": ".", "timeout_sec": 30, "required": True,
            "generated_paths": [".aiqa-output"],
        }],
    }
    project = projects.register_project(admin, "게시판 변경 시험", str(source), "main", policy=policy)
    accounts.set_membership(admin, manager.id, project.id, True, "합성 게시판 시험 담당")
    conversation = start_conversation(
        manager, project.id, (source / "docs" / "requirements.md").read_text(encoding="utf-8"),
        "board-message", "board-conversation",
    )
    work_request = requests.create_or_get_draft(manager, conversation.id)
    specification = {
        "title": "게시판 제목·본문 검색", "goal": "제목이나 본문으로 필요한 게시글을 찾는다.",
        "current_behavior": "목록에 모든 게시글이 표시되고 검색 입력이 없다.",
        "expected_behavior": "검색어가 제목이나 본문에 포함된 게시글만 표시한다.",
        "reproduction_steps": "목록을 열고 제목 일부로 게시글을 찾는다.",
        "scope": "board.py의 목록 화면과 제목·본문 검색", "out_of_scope": "로그인, 작성, 삭제, 배포",
        "unknowns": "", "blocking_questions": [], "attachment_ids": [],
        "related_domains": "", "impact_analysis": "", "conflict_analysis": "",
        "acceptance_criteria": [
            "제목·본문 단일 문자열 부분 일치 검색", "대소문자 무시와 앞뒤 공백 제거",
            "빈 검색어는 전체 목록", "검색 결과가 없으면 안내", "검색어 유지와 HTML 이스케이프",
            "기존 상세와 목록 회귀 검사 통과",
        ],
    }
    board = SimpleNamespace(
        admin=admin, manager=manager, source=source, runtime=runtime, project=project,
        conversation=conversation, request=work_request, specification=specification,
        git=git, policy=policy, baseline_sha=baseline_sha,
    )

    def confirm():
        current = requests.get_request(manager, work_request.id)
        draft = requests.save_draft(manager, work_request.id, current.draft_version, specification)
        basis = requests.get_confirmation_basis(manager, work_request.id)
        return requests.confirm_revision(
            manager, draft.id, draft.draft_version,
            basis.config_revision.project_config_version, basis.base_commit_sha,
        )

    def assert_no_execution(expected_runs):
        assert Run.objects.count() == Job.objects.filter(kind="RUN").count() == expected_runs
        assert RequestSubmission.objects.count() == min(expected_runs, 1)
        assert not ProcessRecord.objects.exists()
        assert not CheckResult.objects.exists()
        assert not ResultBundle.objects.exists()
        assert not AIInvocation.objects.filter(purpose="implementation").exists()
        assert not AIInvocation.objects.exclude(started_at=None).exists()
        assert not Run.objects.exclude(workspace_id="").exists()
        assert not (runtime / "workspaces").exists()
        assert not SandboxCheck.objects.filter(native_passed=True).exists()

    def assert_source_unchanged():
        assert git("rev-parse", "HEAD") == baseline_sha
        assert git("status", "--porcelain=v1", "--untracked-files=all") == ""
        assert git("remote") == ""
        assert {
            path: hashlib.sha256((source / path).read_bytes()).hexdigest() for path in original
        } == original

    board.confirm = confirm
    board.assert_no_execution = assert_no_execution
    board.assert_source_unchanged = assert_source_unchanged
    return board


def test_board_request_is_confirmed_once_and_blocked_run_can_be_retried(board_scenario, monkeypatch):
    def unavailable():
        raise DomainError("DOCKER_UNAVAILABLE")
    monkeypatch.setattr("integrations.codex.container.ready", unavailable)
    board = board_scenario
    revision = board.confirm()
    assert requests.confirm_revision(
        board.manager, board.request.id, revision.source_draft_version,
        revision.project_config_version, revision.base_commit_sha,
    ).id == revision.id
    assert revision.snapshot["acceptance_criteria"] == board.specification["acceptance_criteria"]
    assert revision.base_commit_sha == board.git("rev-parse", "HEAD")

    submission = requests.submit(board.manager, board.request.id, revision.id, "board-submit")
    assert requests.submit(board.manager, board.request.id, revision.id, "board-submit").id == submission.id
    assert requests.submit(board.manager, board.request.id, revision.id, "board-submit-alias").id == submission.id
    assert runs.get_run(board.manager, submission.primary_run_id)["status"] == "QUEUED"
    assert submission.primary_run.profile_snapshot["key"] == "implementation"
    assert submission.primary_run.profile_snapshot["validation_status"] == "UNVERIFIED"

    job = worker.claim_job("EXECUTION", "board-scenario-worker")
    assert job.subject_id == str(submission.primary_run_id)
    worker.process_claimed(job)
    failed = runs.get_run(board.manager, submission.primary_run_id)
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "DOCKER_UNAVAILABLE"
    assert failed["result_bundle_id"] is None

    retry = runs.retry_run(board.manager, submission.primary_run_id, "board-retry")
    assert retry.created and retry.run.previous_run_id == submission.primary_run_id
    assert runs.retry_run(board.manager, submission.primary_run_id, "board-retry").run.id == retry.run.id
    worker.process_claimed(worker.claim_job("EXECUTION", "board-scenario-worker"))
    assert runs.get_run(board.manager, retry.run.id)["error_code"] == "DOCKER_UNAVAILABLE"
    board.assert_no_execution(expected_runs=2)
    board.assert_source_unchanged()


def test_changed_board_baseline_and_policy_need_new_confirmation_without_mutating_old_revision(board_scenario):
    board = board_scenario
    original = board.confirm()
    original_hash = original.content_hash
    board_file = board.source / "board.py"
    board_file.write_text(board_file.read_text() + "\n", encoding="utf-8")
    board.git("add", "board.py")
    board.git("-c", "user.name=Board Fixture", "-c", "user.email=board@localhost", "commit", "-qm", "Updated board baseline")
    next_sha = board.git("rev-parse", "HEAD")

    with pytest.raises(DomainError) as changed_source:
        requests.submit(board.manager, board.request.id, original.id, "stale-source")
    assert changed_source.value.code == "BASE_CHANGED"
    refreshed = requests.refresh_basis(board.manager, board.request.id, original.source_draft_version, "refresh-source")
    assert requests.refresh_basis(
        board.manager, board.request.id, original.source_draft_version, "refresh-source",
    ).draft_version == refreshed.draft_version
    source_revision = requests.confirm_revision(
        board.manager, board.request.id, refreshed.draft_version, original.project_config_version, next_sha,
    )
    assert source_revision.base_commit_sha == next_sha != original.base_commit_sha
    assert source_revision.snapshot == original.snapshot

    policy = {**board.policy, "write_paths": ["board.py", "docs"]}
    configuration = projects.update_configuration(board.admin, board.project.id, 1, policy)
    with pytest.raises(DomainError) as changed_policy:
        requests.submit(board.manager, board.request.id, source_revision.id, "stale-policy")
    assert changed_policy.value.code == "VERSION_CONFLICT"
    refreshed = requests.refresh_basis(board.manager, board.request.id, refreshed.draft_version, "refresh-policy")
    current = requests.confirm_revision(
        board.manager, board.request.id, refreshed.draft_version, configuration.project_config_version, next_sha,
    )
    assert current.number == original.number + 2
    assert current.project_config_version == 2
    assert current.snapshot == original.snapshot
    original.refresh_from_db()
    assert original.base_commit_sha == board.baseline_sha
    assert original.project_config_version == 1
    assert original.content_hash == original_hash
    assert requests.get_request(board.manager, board.request.id).ready_revision_id == current.id
    board.assert_no_execution(expected_runs=0)
    assert board.git("status", "--porcelain=v1", "--untracked-files=all") == ""
    assert board.git("remote") == ""


def test_board_old_tab_cannot_replace_revision_and_revocation_cancels_queued_work(board_scenario):
    board = board_scenario
    first = board.confirm()
    changed_specification = {**board.specification, "goal": "게시글 제목으로 원하는 안내를 빨리 찾는다."}
    draft = requests.save_draft(board.manager, board.request.id, first.source_draft_version, changed_specification)
    with pytest.raises(DomainError) as stale_save:
        requests.save_draft(board.manager, board.request.id, first.source_draft_version, board.specification)
    assert stale_save.value.code == "VERSION_CONFLICT"
    with pytest.raises(DomainError) as stale_confirmation:
        requests.confirm_revision(
            board.manager, board.request.id, first.source_draft_version, 1, first.base_commit_sha,
        )
    assert stale_confirmation.value.code == "VERSION_CONFLICT"
    current = requests.confirm_revision(board.manager, board.request.id, draft.draft_version, 1, first.base_commit_sha)
    assert current.snapshot["goal"] == changed_specification["goal"]
    submission = requests.submit(board.manager, board.request.id, current.id, "before-revocation")

    accounts.set_membership(board.admin, board.manager.id, board.project.id, False, "시험 담당 회수")
    assert runs.get_run(board.admin, submission.primary_run_id)["status"] == "CANCELLED"
    assert not Job.objects.exclude(state="CANCELLED").exists()
    assert worker.claim_job("EXECUTION", "board-scenario-worker") is None
    with pytest.raises(DomainError) as stale_account:
        requests.submit(board.manager, board.request.id, current.id, "before-revocation")
    assert stale_account.value.code == "NOT_FOUND"
    with pytest.raises(DomainError) as admin_impersonation:
        requests.submit(board.admin, board.request.id, current.id, "admin-cannot-own")
    assert admin_impersonation.value.code == "NOT_FOUND"

    accounts.set_membership(board.admin, board.manager.id, board.project.id, True, "시험 담당 재지정")
    replay = requests.submit(board.manager, board.request.id, current.id, "before-revocation")
    assert replay.primary_run_id == submission.primary_run_id
    assert runs.get_run(board.manager, replay.primary_run_id)["status"] == "CANCELLED"
    retry = runs.retry_run(board.manager, replay.primary_run_id, "after-regrant-retry")
    assert retry.run.id != replay.primary_run_id
    assert runs.cancel_run(board.manager, retry.run.id, "end-board-test").status == "CANCELLED"
    first.refresh_from_db()
    assert first.snapshot == board.specification
    board.assert_no_execution(expected_runs=2)
    board.assert_source_unchanged()


@pytest.mark.parametrize("break_title,expected_outcome", [(False, "PASS"), (True, "FAIL")])
def test_registered_board_junit_command_accepts_its_actual_http_evidence(board_scenario, tmp_path, break_title, expected_outcome):
    board = board_scenario
    basis = requests.get_confirmation_basis(board.manager, board.request.id)
    command = basis.policy["verification_commands"][0]
    check_copy = tmp_path / "standalone-board-check"
    shutil.copytree(board.source, check_copy, ignore=shutil.ignore_patterns(".git"))
    if break_title:
        board_file = check_copy / "board.py"
        body = board_file.read_text(encoding="utf-8")
        assert "함께 쓰는 게시판" in body
        board_file.write_text(body.replace("함께 쓰는 게시판", "잘못 변경한 게시판"), encoding="utf-8")
    started = time.time()
    completed = subprocess.run(
        command["argv"], cwd=check_copy,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "empty-home")},
        capture_output=True, text=True, timeout=command["timeout_sec"], check=False,
    )
    assert completed.returncode == int(break_title), completed.stderr
    evidence = assess_check(
        command, completed.returncode, completed.stdout,
        report_path=check_copy / command["report_path"], report_started_at=started,
    )
    assert evidence["outcome"] == expected_outcome, evidence
    assert evidence["collected_count"] == 6
    assert evidence["passed_count"] == 6 - int(break_title)
    assert evidence["failed_count"] == int(break_title)
    assert evidence["skipped_count"] == 0
    board.assert_no_execution(expected_runs=0)
    board.assert_source_unchanged()


@pytest.mark.parametrize("change,error_code", [("source", "BASE_CHANGED"), ("policy", "VERSION_CONFLICT")])
def test_board_worker_rechecks_basis_changed_after_queueing(board_scenario, change, error_code):
    board = board_scenario
    revision = board.confirm()
    submission = requests.submit(board.manager, board.request.id, revision.id, "queued-before-change")
    if change == "source":
        board_file = board.source / "board.py"
        board_file.write_text(board_file.read_text() + "\n", encoding="utf-8")
        board.git("add", "board.py")
        board.git("-c", "user.name=Board Fixture", "-c", "user.email=board@localhost", "commit", "-qm", "Changed while queued")
    else:
        projects.update_configuration(board.admin, board.project.id, 1, {**board.policy, "write_paths": ["board.py", "docs"]})
    current_sha = board.git("rev-parse", "HEAD")

    worker.process_claimed(worker.claim_job("EXECUTION", "board-scenario-worker"))
    status = runs.get_run(board.manager, submission.primary_run_id)
    assert status["status"] == "FAILED"
    assert status["error_code"] == error_code
    with pytest.raises(DomainError) as stale_retry:
        runs.retry_run(board.manager, submission.primary_run_id, "retry-stale-basis")
    assert stale_retry.value.code == error_code
    submission.primary_run.refresh_from_db()
    assert submission.primary_run.baseline_commit_sha == revision.base_commit_sha == board.baseline_sha
    assert submission.primary_run.project_config_version == revision.project_config_version == 1
    board.assert_no_execution(expected_runs=1)
    assert board.git("rev-parse", "HEAD") == current_sha
    assert board.git("status", "--porcelain=v1", "--untracked-files=all") == ""
    assert board.git("remote") == ""
    if change == "policy":
        board.assert_source_unchanged()
