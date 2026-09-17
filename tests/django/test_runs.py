import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.utils import timezone
from django.test import Client

from apps.accounts.models import AccountAudit, ProjectMembership, User
from apps.ai.models import ModelProfile
from apps.conversations.models import Conversation
from apps.errors import DomainError
from apps.projects.models import Project
from apps.runs import processes, services, worker
from apps.runs.models import Job, ProcessRecord, Run, WorkerSlot
from apps.work_requests.models import RequestRevision, WorkRequest


pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_run(monkeypatch, tmp_path):
    owner = User.objects.create_user("manager", password="test-password", display_name="담당자")
    admin = User.objects.create_user("host", password="test-password", display_name="관리자", role="ADMIN")
    outsider = User.objects.create_user("other", password="test-password", display_name="다른 담당자")
    project = Project.objects.create(name="fixture", canonical_repo_path=str(tmp_path), created_by=admin)
    ProjectMembership.objects.create(project=project, user=owner, granted_by=admin)
    conversation = Conversation.objects.create(project=project, owner=owner, title="fix", creation_key="one", creation_hash="a" * 64)
    request = WorkRequest.objects.create(project=project, owner=owner, conversation=conversation, state="READY", draft={"title": "fix"})
    revision = RequestRevision.objects.create(request=request, number=1, source_draft_version=1, snapshot={"title": "fix"},
        content_hash="a" * 64, confirmed_by=owner, project_config_version=1, base_commit_sha="b" * 40)
    request.ready_revision = revision
    request.save(update_fields=["ready_revision"])
    profile, _ = ModelProfile.objects.get_or_create(key="implementation", version=1, defaults={"model_id": "gpt-6-astra", "effort": "xhigh"})
    monkeypatch.setattr("apps.projects.services.resolve_execution_input", lambda *args, **kwargs: SimpleNamespace(project=project))
    monkeypatch.setattr("apps.ai.services.get_profile_snapshot", lambda *args, **kwargs: {
        "id": str(profile.pk), "version": 1, "model_id": profile.model_id, "effort": profile.effort,
    })
    return SimpleNamespace(owner=owner, admin=admin, outsider=outsider, project=project, request=request, revision=revision)


def test_primary_create_and_key_payload_conflicts(setup_run):
    data = setup_run
    first = services.create_run(data.owner, data.revision.pk, "same")
    again = services.create_run(data.owner, data.revision.pk, "same")
    alternate = services.create_run(data.owner, data.revision.pk, "other-key")
    assert first.created and not again.created and alternate.run.pk == first.run.pk
    assert Job.objects.count() == Run.objects.count() == 1
    assert first.run.profile_snapshot["effort"] == "xhigh"
    job = Job.objects.get()
    with pytest.raises(DomainError) as exc:
        services.enqueue_job(kind=job.kind, subject_id=uuid.uuid4(), actor_id=data.owner.pk,
                             project_id=data.project.pk, key="same")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("actor_name", ["admin", "outsider"])
def test_other_account_cannot_submit_even_admin(setup_run, actor_name):
    with pytest.raises(DomainError):
        services.create_run(getattr(setup_run, actor_name), setup_run.revision.pk, "first")
    assert Run.objects.count() == Job.objects.count() == 0


def test_queued_cancel_is_idempotent_and_no_process(setup_run):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    cancelled = services.cancel_run(setup_run.owner, run.pk, "stop")
    assert cancelled.status == "CANCELLED"
    assert services.cancel_run(setup_run.owner, run.pk, "stop").status == "CANCELLED"
    assert Job.objects.get().state == "CANCELLED"
    assert not ProcessRecord.objects.exists()


def test_retry_creates_new_run_and_replayed_key_returns_active_run(setup_run):
    first = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    services.cancel_run(setup_run.owner, first.pk, "stop")
    retried = services.retry_run(setup_run.owner, first.pk, "retry")
    assert retried.created and retried.run.previous_run_id == first.pk
    assert services.retry_run(setup_run.owner, first.pk, "retry").run.pk == retried.run.pk
    with pytest.raises(DomainError) as exc:
        services.retry_run(setup_run.owner, first.pk, "duplicate")
    assert exc.value.code == "RUN_ACTIVE"


def test_claim_two_independent_slots_and_epoch_fencing(setup_run):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    services.enqueue_job(kind="CONVERSATION", subject_id=uuid.uuid4(), actor_id=setup_run.owner.pk, slot="CONVERSATION", key="chat")
    execution = worker.claim_job("EXECUTION", "worker-1")
    conversation = worker.claim_job("CONVERSATION", "worker-1")
    assert execution and conversation
    assert worker.claim_job("EXECUTION", "worker-2") is None
    assert Run.objects.get(pk=run.pk).status == "PREPARING"
    worker.heartbeat(execution)
    with pytest.raises(DomainError) as exc:
        services.transition_run(run.pk, "RUNNING", lease_epoch=execution.lease_epoch + 1)
    assert exc.value.code == "STALE_WORKER"


def test_terminal_requires_all_process_records_stopped(setup_run):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    job = worker.claim_job("EXECUTION", "worker")
    ProcessRecord.objects.create(job=job, run=run, stage="test", worker_identity="worker", instance_id=settings.AIQA_INSTANCE_ID,
                                 host_boot_id="fixture")
    with pytest.raises(DomainError) as exc:
        services.transition_run(run.pk, "FAILED", lease_epoch=job.lease_epoch)
    assert exc.value.code == "PROCESS_TERMINATION_UNCONFIRMED"
    assert Run.objects.get(pk=run.pk).status == "PREPARING"


def test_stale_reservation_allows_new_isolated_retry(setup_run):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    job = worker.claim_job("EXECUTION", "lost")
    ProcessRecord.objects.create(job=job, run=run, stage="test", worker_identity="lost", instance_id=settings.AIQA_INSTANCE_ID,
                                 host_boot_id="fixture")
    WorkerSlot.objects.filter(pk="EXECUTION").update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    worker.recover_stale()
    assert WorkerSlot.objects.get(pk="EXECUTION").blocked_reason == ""
    assert Run.objects.get(pk=run.pk).status == "INTERRUPTED"
    record = ProcessRecord.objects.get()
    assert record.state == "ABANDONED" and record.ended_at is None
    retry = services.retry_run(setup_run.owner, run.pk, "new-isolated-attempt").run
    assert retry.pk != run.pk and retry.previous_run_id == run.pk
    assert worker.claim_job("EXECUTION", "new").subject_id == str(retry.pk)
    from apps.runs.pipeline import _alive
    with pytest.raises(DomainError) as error:
        _alive(job)
    assert error.value.code == "STALE_WORKER"


def test_stale_without_children_is_interrupted_not_replayed(setup_run):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    worker.claim_job("EXECUTION", "lost")
    WorkerSlot.objects.filter(pk="EXECUTION").update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    worker.recover_stale()
    assert Run.objects.get(pk=run.pk).status == "INTERRUPTED"
    assert Job.objects.get().state == "FAILED"


def test_revoke_queued_actor_work_cancels_all_without_process(setup_run):
    services.create_run(setup_run.owner, setup_run.revision.pk, "start")
    services.revoke_actor_work(setup_run.owner.pk, setup_run.project.pk)
    assert Run.objects.get().status == "CANCELLED"
    assert Job.objects.get().state == "CANCELLED"


def test_status_poll_never_invokes_ai_and_current_acl_applies(setup_run, monkeypatch):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    monkeypatch.setattr("apps.ai.services.invoke", lambda *a, **k: pytest.fail("poll invoked AI"))
    assert services.get_run(setup_run.owner, run.pk)["status"] == "QUEUED"
    assert services.get_run(setup_run.admin, run.pk)["status"] == "QUEUED"
    ProjectMembership.objects.filter(user=setup_run.owner).delete()
    with pytest.raises(DomainError):
        services.get_run(setup_run.owner, run.pk)


def test_process_gate_is_recorded_before_command_runs(setup_run, tmp_path, monkeypatch):
    job = services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="command")
    job = worker.claim_job("EXECUTION", "worker")
    target = tmp_path / "ran"
    original_save = ProcessRecord.save
    observed = []

    def save(record, *args, **kwargs):
        if record.state == "REGISTERED":
            observed.append(not target.exists())
        return original_save(record, *args, **kwargs)

    monkeypatch.setattr(ProcessRecord, "save", save)
    result = processes.launch_process(job=job, argv=[sys.executable, "-I", "-c", "from pathlib import Path; Path('ran').touch(); print('fixture')"],
                                      cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "fixture"
    assert observed == [True] and target.exists()
    record = ProcessRecord.objects.get()
    assert record.run_id is None and record.ended_at and record.process_start_identity


def test_closed_output_streams_do_not_shorten_deadline(setup_run, tmp_path):
    services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="command")
    job = worker.claim_job("EXECUTION", "worker")
    started = time.monotonic()
    result = processes.launch_process(job=job, argv=[sys.executable, "-I", "-c", "import os,time; os.close(1); os.close(2); time.sleep(1.4)"],
                                      cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, timeout=5)
    assert result.returncode == 0 and not result.timed_out
    assert time.monotonic() - started >= 1.4


def test_timeout_terminates_and_confirms_fixture(setup_run, tmp_path):
    services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="command")
    job = worker.claim_job("EXECUTION", "worker")
    result = processes.launch_process(job=job, argv=[sys.executable, "-I", "-c", "import time; time.sleep(20)"],
                                      cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, timeout=0.3)
    assert result.timed_out and result.returncode != 0
    assert ProcessRecord.objects.get().ended_at is not None


def test_reused_pid_never_receives_a_signal(setup_run, monkeypatch):
    job = services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="command")
    job = worker.claim_job("EXECUTION", "worker")
    record = ProcessRecord.objects.create(job=job, stage="test", worker_identity="worker", instance_id=settings.AIQA_INSTANCE_ID,
        host_boot_id="boot", pid=123, process_group_id=123, process_start_identity="old")
    monkeypatch.setattr(processes, "host_boot_id", lambda: "boot")
    monkeypatch.setattr(processes, "process_table", lambda: {123: (1, 123, "different")})
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("reused PID signalled"))
    with pytest.raises(DomainError) as exc:
        processes.terminate_record(record)
    assert exc.value.code == "PROCESS_TERMINATION_UNCONFIRMED"
    assert WorkerSlot.objects.get().blocked_reason == "EXECUTION_BLOCKED"


def test_sandbox_failure_happens_before_source_or_target_execution(setup_run, monkeypatch):
    from apps.runs import pipeline

    services.create_run(setup_run.owner, setup_run.revision.pk, "start")
    job = worker.claim_job("EXECUTION", "worker")
    monkeypatch.setattr("apps.projects.services.resolve_execution_input", lambda *a, **k: SimpleNamespace(config_revision=None, policy={"verification_commands": [{"required": True}]}))
    def unavailable(*args, **kwargs):
        raise DomainError("SANDBOX_UNAVAILABLE")
    monkeypatch.setattr("apps.ai.services.require_sandbox_ready", unavailable)
    monkeypatch.setattr(pipeline, "create_workspace", lambda *a, **k: pytest.fail("source prepared before sandbox"))
    monkeypatch.setattr(pipeline, "launch_process", lambda *a, **k: pytest.fail("target executed before sandbox"))
    worker.process_claimed(job)
    assert Run.objects.get().status == "FAILED"
    assert Run.objects.get().error_code == "SANDBOX_UNAVAILABLE"
    assert not ProcessRecord.objects.exists()


@pytest.mark.parametrize("after_fails", [False, True])
def test_fixture_pipeline_seals_only_actual_required_after_pass(setup_run, tmp_path, monkeypatch, settings, after_fails):
    from apps.runs import pipeline
    from apps.artifacts.models import ResultBundle

    repo = tmp_path / "source"
    repo.mkdir()
    data_root = tmp_path / "runtime"
    data_root.mkdir()
    settings.AIQA_DATA_DIR = data_root
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    def git(*args):
        result = subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                                env=environment, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    git("init", "-q")
    (repo / "main.py").write_text("value = 1\n")
    git("add", "main.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture")
    sha = git("rev-parse", "HEAD")
    policy = {"read_paths": ["."], "write_paths": ["."], "excluded_paths": [], "preparation_commands": [],
              "verification_commands": [{"command_id": "unit", "kind": "TEST", "parser_id": "pytest-summary", "parser_version": 1,
                  "argv": ["registered-fixture"], "cwd": ".", "timeout_sec": 5, "required": True, "generated_paths": [".aiqa-output"]}]}
    config = SimpleNamespace(canonical_repo_path=str(repo), read_paths=["."], write_paths=["."], excluded_paths=[])
    monkeypatch.setattr("apps.projects.services.resolve_execution_input", lambda *a, **k: SimpleNamespace(policy=policy, config_revision=config))
    monkeypatch.setattr("apps.ai.services.require_sandbox_ready", lambda *a, **k: None)
    original_revision = setup_run.revision
    original_revision.base_commit_sha = sha
    # The immutable revision manager intentionally rejects updates; the fixture is created afresh.
    request = setup_run.request
    request.draft_version = 2
    revision = RequestRevision.objects.create(request=request, number=2, source_draft_version=2, snapshot={"title": "fix", "acceptance_criteria": ["value는 2"]},
        content_hash="a" * 64, confirmed_by=setup_run.owner, project_config_version=1, base_commit_sha=sha)
    request.ready_revision = revision
    request.save(update_fields=["ready_revision", "draft_version"])
    run = services.create_run(setup_run.owner, revision.pk, "pipeline").run
    job = worker.claim_job("EXECUTION", "worker")
    phases = []
    def check(job, source, command, process_launcher):
        phases.append("AFTER" if "after-checks" in str(source) else "BASELINE")
        failure = after_fails and phases[-1] == "AFTER"
        return processes.ProcessResult(1 if failure else 0, "1 failed in 0.01s" if failure else "1 passed in 0.01s", "", True, False)
    monkeypatch.setattr("apps.ai.services.execute_check", check)
    def modify(job, workspace, run, policy):
        (workspace / "main.py").write_text("value = 2\n")
        return SimpleNamespace(final_response="변경 보고: main.py의 value만 수정. 엔드포인트와 데이터 변경 없음")
    monkeypatch.setattr(pipeline, "_modify", modify)
    worker.process_claimed(job)
    run.refresh_from_db()
    assert phases == ["BASELINE", "AFTER"]
    assert (repo / "main.py").read_text() == "value = 1\n"
    assert git("status", "--porcelain") == ""
    assert run.status == ("FAILED" if after_fails else "SUCCEEDED"), run.error_code
    assert ResultBundle.objects.count() == (0 if after_fails else 1)
    if not after_fails:
        assert run.result_bundle_id is not None
        assert run.result_branch == f"temp/aiqa-{run.pk}"
        assert run.result_commit_sha
        workspace = data_root / "workspaces" / str(run.pk)
        assert subprocess.check_output(["git", "-C", str(workspace), "show", "HEAD:main.py"]).decode() == "value = 2\n"
        assert run.checks.get(phase="AFTER").outcome == "PASS"
        assert "main.py의 value만 수정" in ResultBundle.objects.get(pk=run.result_bundle_id).summary
        import json
        from apps.artifacts.services import read_bundle_file
        evaluation = json.loads(read_bundle_file(run.result_bundle_id, "evaluation.json"))
        assert evaluation["requirements"] == [{"id": "AC-1", "criterion": "value는 2", "assessment": "NEEDS_REVIEW"}]
        assert evaluation["required_checks"] == "PASS"
    else:
        assert not run.result_commit_sha


def test_revoke_active_conversation_keeps_busy_until_stopped(setup_run):
    services.enqueue_job(kind="CONVERSATION", subject_id=uuid.uuid4(), actor_id=setup_run.owner.pk, project_id=setup_run.project.pk,
                         slot="CONVERSATION", key="chat")
    job = worker.claim_job("CONVERSATION", "worker")
    services.revoke_actor_work(setup_run.owner.pk, setup_run.project.pk)
    job.refresh_from_db()
    assert job.state == "RUNNING"
    assert job.cancel_requested_at and job.cancel_reason == "ACCESS_REVOKED"
    assert processes._cancelled(job)


def test_old_revision_retry_blocked_when_new_revision_active(setup_run):
    data = setup_run
    original = services.create_run(data.owner, data.revision.pk, "first").run
    services.cancel_run(data.owner, original.pk, "cancel")
    revision = RequestRevision.objects.create(request=data.request, number=2, source_draft_version=2, snapshot={"title": "new"},
        content_hash="c" * 64, confirmed_by=data.owner, project_config_version=1, base_commit_sha="b" * 40)
    data.request.ready_revision = revision
    data.request.save(update_fields=["ready_revision"])
    services.create_run(data.owner, revision.pk, "second")
    with pytest.raises(DomainError) as exc:
        services.retry_run(data.owner, original.pk, "old-retry")
    assert exc.value.code == "RUN_ACTIVE"


def test_restored_run_requires_new_confirmation(setup_run):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    services.cancel_run(setup_run.owner, run.pk, "stop")
    Job.objects.filter(subject_id=str(run.pk)).update(instance_id="old-instance")
    with pytest.raises(DomainError) as exc:
        services.retry_run(setup_run.owner, run.pk, "retry")
    assert exc.value.code == "REQUEST_RECONFIRM_REQUIRED"
    assert Run.objects.count() == 1


def test_run_http_poll_and_csrf_boundary(setup_run):
    data = setup_run
    run = services.create_run(data.owner, data.revision.pk, "start").run
    client = Client(enforce_csrf_checks=True)
    client.force_login(data.owner)
    session = client.session
    session["issued_at"] = timezone.now().timestamp()
    session["authz_version"] = data.owner.authz_version
    session.save()
    detail = client.get(f"/runs/{run.pk}/")
    assert detail.status_code == 200
    assert detail.context["work_request"].id == data.request.id
    assert detail.context["work_request"].title == data.request.title
    assert "수정 → 검증 → 결과" in detail.content.decode()
    response = client.get(f"/runs/{run.pk}/status/", HTTP_ACCEPT="application/json")
    assert response.status_code == 200 and response.json()["status"] == "QUEUED"
    assert set(response.json()) == {
        "id", "status", "label", "stage", "state_version", "elapsed_seconds", "events",
        "result_bundle_id", "error_code", "message", "terminal", "is_owner",
    }
    assert "no-store" in response["Cache-Control"].split(", ")
    assert client.get(f"/runs/{run.pk}/cancel/").status_code == 405
    assert client.post(f"/runs/{run.pk}/cancel/", {"key": "cancel"}).status_code == 403
    token = client.cookies["csrftoken"].value
    assert client.post(f"/runs/{run.pk}/cancel/", {"key": "cancel", "csrfmiddlewaretoken": token}).status_code == 302
    run.refresh_from_db()
    assert run.status == "CANCELLED"


@pytest.mark.parametrize("access", ["outsider", "revoked", "anonymous"])
def test_run_parent_request_is_not_exposed_without_access(setup_run, access):
    data = setup_run
    run = services.create_run(data.owner, data.revision.pk, "start").run
    client = Client()
    if access != "anonymous":
        actor = data.owner if access == "revoked" else data.outsider
        client.force_login(actor)
        session = client.session
        session["issued_at"] = timezone.now().timestamp()
        session["authz_version"] = actor.authz_version
        session.save()
    if access == "revoked":
        ProjectMembership.objects.filter(user=data.owner).delete()

    detail = client.get(f"/runs/{run.pk}/")

    assert detail.status_code == (302 if access == "anonymous" else 404)
    assert detail.context is None or "work_request" not in detail.context
    assert str(data.request.id) not in detail.content.decode()
    assert data.request.title not in detail.content.decode()
    status = client.get(f"/runs/{run.pk}/status/", HTTP_ACCEPT="application/json")
    assert status.status_code == (401 if access == "anonymous" else 404)
    assert str(data.request.id) not in status.content.decode()
    assert data.request.title not in status.content.decode()


def test_run_parent_navigation_keeps_one_admin_view_audit(setup_run):
    data = setup_run
    run = services.create_run(data.owner, data.revision.pk, "start").run
    client = Client()
    client.force_login(data.admin)
    session = client.session
    session["issued_at"] = timezone.now().timestamp()
    session["authz_version"] = data.admin.authz_version
    session.save()
    previous_audits = AccountAudit.objects.filter(actor=data.admin).count()

    detail = client.get(f"/runs/{run.pk}/")

    assert detail.status_code == 200
    assert detail.context["work_request"].id == data.request.id
    assert detail.context["work_request"].title == data.request.title
    assert AccountAudit.objects.filter(actor=data.admin).count() == previous_audits + 1
    audit = AccountAudit.objects.filter(actor=data.admin).latest("created_at")
    assert audit.action == "run.view" and audit.subject_id == data.owner.id


def test_original_fingerprint_detects_dirty_index_and_preserves_files(tmp_path):
    from apps.runs.source_guard import repository_fingerprint

    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}, capture_output=True, check=True)
    git("init", "-q")
    (repo / "code.txt").write_text("committed")
    git("add", "code.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture")
    first = repository_fingerprint(repo)
    assert repository_fingerprint(repo) == first
    (repo / "code.txt").write_text("dirty")
    dirty = repository_fingerprint(repo)
    assert dirty != first and dirty["index"] == first["index"]
    git("add", "code.txt")
    staged = repository_fingerprint(repo)
    assert staged["index"] != dirty["index"]
    (repo / "new.txt").write_text("user-owned")
    assert repository_fingerprint(repo) != staged
    assert (repo / "code.txt").read_text() == "dirty"
    assert (repo / "new.txt").read_text() == "user-owned"


@pytest.mark.parametrize("limits,source", [
    ({"stdout_line_limit": 64}, "import sys,time; sys.stdout.write('x'*65); sys.stdout.flush(); time.sleep(5)"),
    ({"output_limit": 64}, "import sys,time; sys.stdout.write('x\\n'*40); sys.stdout.flush(); time.sleep(5)"),
    ({"stdout_line_limit": 64}, "import sys,time; sys.stdout.write('x'*40); sys.stdout.flush(); time.sleep(.2); sys.stdout.write('x'*40); sys.stdout.flush(); time.sleep(5)"),
])
def test_stream_limits_stop_before_process_deadline(setup_run, tmp_path, limits, source):
    services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="command")
    job = worker.claim_job("EXECUTION", "worker")
    started = time.monotonic()
    result = processes.launch_process(job=job, argv=[sys.executable, "-I", "-c", source], cwd=tmp_path,
                                      env={"PATH": "/usr/bin:/bin"}, timeout=10, **limits)
    assert not result.output_complete and not result.timed_out
    assert time.monotonic() - started < 4
    assert ProcessRecord.objects.get().ended_at


def test_line_limit_resets_at_record_boundary(setup_run, tmp_path):
    services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="command")
    job = worker.claim_job("EXECUTION", "worker")
    result = processes.launch_process(job=job, argv=[sys.executable, "-I", "-c", "print('x'*64); print('y'*64)"],
                                      cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, timeout=5, stdout_line_limit=64)
    assert result.output_complete and result.returncode == 0


@pytest.mark.parametrize("stopped", [False, True])
def test_modification_invocation_not_terminal_until_processes_stopped(setup_run, tmp_path, monkeypatch, stopped):
    from apps.ai.models import AIInvocation
    from apps.runs.pipeline import _modify

    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    job = worker.claim_job("EXECUTION", "worker")
    run = services.transition_run(run.pk, "RUNNING", lease_epoch=job.lease_epoch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ProcessRecord.objects.create(job=job, run=run, stage="RUN", worker_identity="worker", instance_id=settings.AIQA_INSTANCE_ID,
                                 host_boot_id="fixture", ended_at=timezone.now() if stopped else None)
    def invocation_error(*args, **kwargs):
        raise DomainError("PROCESS_TERMINATION_UNCONFIRMED")
    monkeypatch.setattr("apps.ai.services.invoke", invocation_error)
    with pytest.raises(DomainError):
        _modify(job, workspace, run, {"read_paths": ["."], "write_paths": ["."]})
    invocation = AIInvocation.objects.get(run=run)
    assert invocation.state == ("FAILED" if stopped else "RUNNING")
    assert (invocation.ended_at is not None) is stopped
    assert invocation.error_code == "PROCESS_TERMINATION_UNCONFIRMED"


def test_worker_lock_rejects_symlink_without_changing_target(tmp_path):
    from django.core.management.base import CommandError
    from apps.runs.management.commands.process_jobs import worker_lock

    target = tmp_path / "unrelated.txt"
    target.write_text("user-owned content")
    target.chmod(0o644)
    before = target.stat()
    link = tmp_path / "worker.lock"
    link.symlink_to(target)
    with pytest.raises(CommandError):
        with worker_lock(link):
            pytest.fail("symlink worker lock accepted")
    assert target.read_text() == "user-owned content"
    assert target.stat().st_mode == before.st_mode


def test_worker_lock_rejects_shared_permissions_without_chmod(tmp_path):
    from django.core.management.base import CommandError
    from apps.runs.management.commands.process_jobs import worker_lock

    path = tmp_path / "worker.lock"
    path.write_text("existing")
    path.chmod(0o644)
    before = path.stat()
    with pytest.raises(CommandError):
        with worker_lock(path):
            pytest.fail("public worker lock accepted")
    assert path.read_text() == "existing"
    assert path.stat().st_mode == before.st_mode


def test_worker_lock_is_exclusive_and_reusable_after_release(tmp_path):
    from django.core.management.base import CommandError
    from apps.runs.management.commands.process_jobs import worker_lock

    path = tmp_path / "worker.lock"
    with worker_lock(path):
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(CommandError):
            with worker_lock(path):
                pytest.fail("second worker entered")
    with worker_lock(path):
        assert path.read_bytes() == b""


@pytest.mark.parametrize("kind,slot", [("RUN", "EXECUTION"), ("CONVERSATION", "CONVERSATION"), ("publication.prepare", "EXECUTION")])
def test_new_jobs_rejected_when_space_below_two_gib(setup_run, monkeypatch, tmp_path, kind, slot):
    existing_result = tmp_path / "existing-result.txt"
    existing_result.write_text("preserve this evidence")
    monkeypatch.setattr(services.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024 ** 3 - 1))
    with pytest.raises(DomainError) as exc:
        services.enqueue_job(kind=kind, subject_id=uuid.uuid4(), actor_id=setup_run.owner.pk, slot=slot, key="new")
    assert exc.value.code == "DISK_SPACE_LOW" and exc.value.status == 507
    assert not Job.objects.exists()
    assert existing_result.read_text() == "preserve this evidence"


def test_admission_boundary_accepts_exactly_two_gib_and_preserves_replay(setup_run, monkeypatch):
    monkeypatch.setattr(services.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024 ** 3))
    job = services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="existing")
    monkeypatch.setattr(services.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    replay = services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="existing")
    assert replay.pk == job.pk and Job.objects.count() == 1


def test_storage_probe_failure_is_fail_closed(setup_run, monkeypatch):
    def unavailable(path):
        raise OSError("fixture unavailable")
    monkeypatch.setattr(services.shutil, "disk_usage", unavailable)
    with pytest.raises(DomainError) as exc:
        services.enqueue_job(kind="fixture", subject_id="test", actor_id=setup_run.owner.pk, key="new")
    assert exc.value.code == "STORAGE_UNAVAILABLE"
    assert not Job.objects.exists()


def test_run_submission_rolls_back_when_admission_space_is_low(setup_run, monkeypatch):
    monkeypatch.setattr(services.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(DomainError):
        services.create_run(setup_run.owner, setup_run.revision.pk, "start")
    assert not Run.objects.exists() and not Job.objects.exists()


def test_worker_rechecks_space_before_run_side_effects(setup_run, monkeypatch, tmp_path):
    run = services.create_run(setup_run.owner, setup_run.revision.pk, "start").run
    job = worker.claim_job("EXECUTION", "worker")
    source = tmp_path / "source.txt"
    source.write_text("source is unchanged")
    monkeypatch.setattr(services.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(worker, "_dispatch", lambda job: pytest.fail("handler ran below disk threshold"))
    monkeypatch.setattr(processes, "launch_process", lambda **kwargs: pytest.fail("CLI ran below disk threshold"))
    worker.process_claimed(job)
    run.refresh_from_db()
    job.refresh_from_db()
    assert run.status == "FAILED" and run.error_code == "DISK_SPACE_LOW"
    assert job.state == "FAILED" and job.error_code == "DISK_SPACE_LOW"
    assert not ProcessRecord.objects.exists()
    assert source.read_text() == "source is unchanged"


def test_worker_marks_unstarted_ai_failed_without_model_call(setup_run, monkeypatch):
    from apps.ai.models import AIInvocation

    invocation = AIInvocation.objects.create(actor=setup_run.owner, purpose="conversation")
    services.enqueue_job(kind="CONVERSATION", subject_id=invocation.pk, actor_id=setup_run.owner.pk, slot="CONVERSATION", key="chat")
    job = worker.claim_job("CONVERSATION", "worker")
    monkeypatch.setattr(services.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(worker, "_dispatch", lambda job: pytest.fail("AI handler ran below disk threshold"))
    worker.process_claimed(job)
    invocation.refresh_from_db()
    assert invocation.state == "FAILED" and invocation.error_code == "DISK_SPACE_LOW"
    assert invocation.ended_at and not ProcessRecord.objects.exists()


def test_worker_blocks_publication_preparation_before_remote_dispatch(setup_run, monkeypatch):
    services.enqueue_job(kind="publication.prepare", subject_id=uuid.uuid4(), actor_id=setup_run.admin.pk, slot="EXECUTION", key="publish")
    job = worker.claim_job("EXECUTION", "worker")
    monkeypatch.setattr(services.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(worker, "_dispatch", lambda job: pytest.fail("remote handler ran below disk threshold"))
    monkeypatch.setattr(processes, "launch_process", lambda **kwargs: pytest.fail("remote CLI ran below disk threshold"))
    worker.process_claimed(job)
    job.refresh_from_db()
    assert job.state == "FAILED" and job.error_code == "DISK_SPACE_LOW"
    assert not ProcessRecord.objects.exists()
