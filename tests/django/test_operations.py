import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import closing
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.ai.models import AIInvocation, ModelProfile, SandboxCheck
from apps.approvals.models import Approval, Publication, PublicationPolicy
from apps.artifacts.models import Artifact, ResultBundle
from apps.conversations.models import Conversation
from apps.errors import DomainError
from apps.operations import backup, doctor
from apps.projects.models import DependencyBundle, Project, ProjectHealth
from apps.runs.models import Job, ProcessRecord, Run, WorkerSlot
from apps.work_requests.models import RequestRevision, WorkRequest
from config.runtime import runtime_lock


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def runtime(tmp_path, settings):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    settings.AIQA_DATA_DIR = root
    settings.AIQA_INSTANCE_ID = str(uuid.uuid4())
    for name, value in (("secret-key", "test-application-secret"), ("instance-id", settings.AIQA_INSTANCE_ID), (".aiqa-runtime", "aiqa-local-v1")):
        file = root / name
        file.write_text(value + "\n")
        file.chmod(0o600)
    for name in backup.DATA_DIRECTORIES:
        (root / name).mkdir(mode=0o700)

    def snapshot():
        database_path = root / "db.sqlite3"
        with closing(sqlite3.connect(database_path)) as destination:
            connection.ensure_connection()
            connection.connection.backup(destination)
        database_path.chmod(0o600)

    return SimpleNamespace(root=root, instance_id=settings.AIQA_INSTANCE_ID, snapshot=snapshot)


@pytest.fixture
def records(runtime, tmp_path):
    admin = get_user_model().objects.create_user(username="operator", password="Operations-test-password-123", role="ADMIN", display_name="관리자")
    project = Project.objects.create(name="Example", canonical_repo_path=str(tmp_path / "source"), created_by=admin)
    conversation = Conversation.objects.create(project=project, owner=admin, creation_key="one", creation_hash="a" * 64, title="원본 요청")
    request = WorkRequest.objects.create(project=project, owner=admin, conversation=conversation)
    revision = RequestRevision.objects.create(request=request, number=1, source_draft_version=1, snapshot={}, content_hash="b" * 64, confirmed_by=admin, project_config_version=1, base_commit_sha="c" * 40)
    run = Run.objects.create(project=project, request_revision=revision, requested_by=admin, baseline_commit_sha="c" * 40, project_config_version=1, model_profile_version=1, idempotency_key="run-one")
    job = Job.objects.create(kind="RUN", subject_id=str(run.pk), actor=admin, project=project, idempotency_key="job-one", payload_hash="d" * 64, instance_id=runtime.instance_id)
    return SimpleNamespace(admin=admin, project=project, conversation=conversation, request=request, revision=revision, run=run, job=job)


def assert_error(code, operation, *args, **kwargs):
    with pytest.raises(DomainError) as error:
        operation(*args, **kwargs)
    assert error.value.code == code


def test_backup_preserves_files_and_excludes_provider_credentials(runtime):
    (runtime.root / "uploads" / "attachment.txt").write_text("보존할 첨부")
    (runtime.root / ".codex").mkdir()
    (runtime.root / ".codex" / "auth.json").write_text("NEVER_READ_PROVIDER_SECRET")
    (runtime.root / "workspaces").mkdir()
    (runtime.root / "workspaces" / "code.txt").write_text("not included")
    runtime.snapshot()
    target = backup.create_backup()
    manifest = backup.verify_backup(target)
    assert {row["path"] for row in manifest["files"]} == {"db.sqlite3", "secret-key", "instance-id", ".aiqa-runtime", "uploads/attachment.txt"}
    assert "provider credentials" in manifest["excluded"]
    assert "NEVER_READ_PROVIDER_SECRET" not in (target / "manifest.json").read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    for file in target.rglob("*"):
        if file.is_file():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_backup_refuses_active_writer_without_creating_snapshot(runtime):
    runtime.snapshot()
    destination = runtime.root / "backups" / "blocked"
    with runtime_lock(runtime.root):
        with pytest.raises(RuntimeError, match="Runtime is busy"):
            backup.create_backup(destination)
    assert not destination.exists()


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_backup_can_be_reverified_and_restored_from_real_sqlite_journal_modes(runtime, tmp_path, journal_mode):
    runtime.snapshot()
    with closing(sqlite3.connect(runtime.root / "db.sqlite3")) as database:
        assert database.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0].upper() == journal_mode
    target = backup.create_backup()
    manifest = backup.verify_backup(target)
    assert (target / "files" / "db.sqlite3").read_bytes()[18:20] == b"\x01\x01"
    assert {file.relative_to(target).as_posix() for file in target.rglob("*") if file.is_file()} == {"manifest.json", *("files/" + row["path"] for row in manifest["files"])}
    backup.verify_backup(target)
    restored = backup.restore_backup(target, tmp_path / "restored")
    assert (restored / "restore-report.json").exists()


def test_backup_includes_committed_wal_records_without_altering_source_journal(runtime, records):
    runtime.snapshot()
    with closing(sqlite3.connect(runtime.root / "db.sqlite3")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE projects_project SET name='committed-in-wal'")
        writer.commit()
        assert (runtime.root / "db.sqlite3-wal").stat().st_size > 0
        target = backup.create_backup()
        with closing(backup._database(target / "files" / "db.sqlite3", immutable=True)) as restored:
            assert restored.execute("SELECT name FROM projects_project").fetchone()[0] == "committed-in-wal"
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        backup.verify_backup(target)
    assert not (target / "files" / "db.sqlite3-wal").exists()
    assert not (target / "files" / "db.sqlite3-shm").exists()


def test_snapshot_verification_does_not_create_sidecars_for_wal_header(runtime):
    runtime.snapshot()
    target = backup.create_backup()
    database_path = target / "files" / "db.sqlite3"
    with closing(sqlite3.connect(database_path)) as database:
        database.execute("PRAGMA journal_mode=WAL")
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"]:
        if entry["path"] == "db.sqlite3":
            entry["sha256"] = hashlib.sha256(database_path.read_bytes()).hexdigest()
            entry["size_bytes"] = database_path.stat().st_size
    manifest_path.write_text(json.dumps(manifest))
    original_files = {file.relative_to(target).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest() for file in target.rglob("*") if file.is_file()}
    backup.verify_backup(target)
    backup.verify_backup(target)
    assert {file.relative_to(target).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest() for file in target.rglob("*") if file.is_file()} == original_files


@pytest.mark.parametrize("record_state", ["RESERVED", "RUNNING", "UNKNOWN"])
def test_backup_refuses_incomplete_process_identity(runtime, records, monkeypatch, record_state):
    ProcessRecord.objects.create(job=records.job, stage="RUN", worker_identity="old-worker", instance_id=runtime.instance_id, host_boot_id="same-boot", state=record_state)
    runtime.snapshot()
    monkeypatch.setattr(backup, "host_boot_id", lambda: "same-boot")
    monkeypatch.setattr(backup, "process_table", lambda: {})
    assert_error("PROCESS_TERMINATION_UNCONFIRMED", backup.create_backup)


def test_backup_refuses_alive_or_reused_pid_and_never_signals(runtime, records, monkeypatch):
    ProcessRecord.objects.create(job=records.job, stage="RUN", worker_identity="old-worker", instance_id=runtime.instance_id, host_boot_id="same-boot", pid=87654, process_group_id=87654, process_start_identity="original-start", state="RUNNING")
    runtime.snapshot()
    monkeypatch.setattr(backup, "host_boot_id", lambda: "same-boot")
    monkeypatch.setattr(backup, "process_table", lambda: {87654: (1, 87654, "different-start")})
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("backup must never terminate a process"))
    assert_error("PROCESS_TERMINATION_UNCONFIRMED", backup.create_backup)


def test_backup_allows_old_boot_process_but_keeps_source_record(runtime, records, monkeypatch):
    record = ProcessRecord.objects.create(job=records.job, stage="RUN", worker_identity="old-worker", instance_id=runtime.instance_id, host_boot_id="previous-boot", state="RESERVED")
    runtime.snapshot()
    monkeypatch.setattr(backup, "host_boot_id", lambda: "current-boot")
    monkeypatch.setattr(backup, "process_table", lambda: {})
    backup.create_backup()
    record.refresh_from_db()
    assert record.state == "RESERVED" and record.ended_at is None


def test_backup_rejects_symlink_without_reading_target(runtime, tmp_path):
    sensitive = tmp_path / "outside-auth.json"
    sensitive.write_text("PRIVATE")
    (runtime.root / "uploads" / "link").symlink_to(sensitive)
    runtime.snapshot()
    assert_error("BACKUP_PATH_INVALID", backup.create_backup)
    assert sensitive.read_text() == "PRIVATE"


def test_backup_rejects_missing_db_referenced_artifact(runtime, records):
    Artifact.objects.create(project=records.project, owner=records.admin, conversation=records.conversation, kind="INPUT_ATTACHMENT", storage_key="uploads/missing/file.txt", sha256="f" * 64, size_bytes=3, mime_type="text/plain", display_name="file.txt", expires_at=timezone.now() + timedelta(days=1))
    runtime.snapshot()
    assert_error("BACKUP_REFERENCE_MISSING", backup.create_backup)


@pytest.mark.parametrize("change", ["checksum", "traversal", "duplicate", "extra_file"])
def test_manifest_rejects_tampered_or_escaping_backup(runtime, change):
    runtime.snapshot()
    target = backup.create_backup()
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "checksum":
        (target / "files" / "secret-key").write_text("changed")
        code = "BACKUP_CHECKSUM_MISMATCH"
    elif change == "traversal":
        manifest["files"][0]["path"] = "../escaped"
        code = "BACKUP_INVALID"
    elif change == "duplicate":
        manifest["files"].append(manifest["files"][0])
        code = "BACKUP_INVALID"
    else:
        (target / "files" / "unlisted").write_text("extra")
        code = "BACKUP_INVALID"
    manifest_path.write_text(json.dumps(manifest))
    assert_error(code, backup.verify_backup, target)


def test_restore_invalidates_authority_without_replaying_and_preserves_completed(runtime, records, tmp_path, monkeypatch):
    now = timezone.now()
    Session.objects.create(session_key="old-login", session_data="old-session", expire_date=now + timedelta(days=1))
    profile = ModelProfile.objects.create(key="implementation", version=1, model_id="gpt-6-astra", effort="xhigh", validation_status="VALIDATED", validated_binary_hash="a" * 64)
    AIInvocation.objects.create(actor=records.admin, purpose="implementation", profile=profile, state="RUNNING")
    completed = AIInvocation.objects.create(actor=records.admin, purpose="conversation", state="SUCCEEDED", final_response="완료된 과거 응답")
    SandboxCheck.objects.create(cli_binary_hash="a" * 64, cli_version="test", policy_hash="b" * 64, host_os_version="test", native_passed=True, file_tools_passed=True)
    ProjectHealth.objects.create(project=records.project, readiness="READY", source_ready=True, modification_ready=True)
    WorkerSlot.objects.create(name="EXECUTION", job=records.job, owner="old-worker", epoch=3)
    bundle = ResultBundle.objects.create(run=records.run, request_revision=records.revision, version=1, baseline_commit_sha="c" * 40, project_config_version=1, manifest_sha256="e" * 64, result_tree_sha256="f" * 64, sealed_at=now, expires_at=now + timedelta(days=30))
    policy = PublicationPolicy.objects.create(project=records.project, updated_by=records.admin)
    publication = Publication.objects.create(project=records.project, run=records.run, bundle=bundle, policy=policy, state="PUBLISHING", snapshot={}, snapshot_hash="f" * 64, created_by=records.admin)
    Approval.objects.create(publication=publication, actor=records.admin, decision="APPROVE", snapshot_hash="f" * 64)
    completed_job = Job.objects.create(kind="CONVERSATION", subject_id="old", actor=records.admin, idempotency_key="completed", payload_hash="e" * 64, instance_id=runtime.instance_id, state="DONE", finished_at=now, lease_owner="historic")
    runtime.snapshot()
    source_checksum = hashlib.sha256((runtime.root / "db.sqlite3").read_bytes()).hexdigest()
    target = backup.create_backup()
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("restore must never terminate a process"))
    restored = backup.restore_backup(target, tmp_path / "restored")
    with closing(sqlite3.connect(restored / "db.sqlite3")) as database:
        assert database.execute("SELECT count(*) FROM django_session").fetchone()[0] == 0
        assert database.execute("SELECT valid,invalid_reason FROM approvals_approval").fetchone() == (0, "RESTORED_SNAPSHOT")
        assert database.execute("SELECT state,error_code FROM approvals_publication").fetchone() == ("UNKNOWN", "RESTORED_SNAPSHOT")
        assert database.execute("SELECT state,error_code,lease_owner FROM runs_job WHERE id=?", (records.job.pk.hex,)).fetchone() == ("CANCELLED", "RESTORED_SNAPSHOT", "")
        assert database.execute("SELECT state,lease_owner FROM runs_job WHERE id=?", (completed_job.pk.hex,)).fetchone() == ("DONE", "historic")
        assert database.execute("SELECT status FROM runs_run").fetchone()[0] == "CANCELLED"
        assert database.execute("SELECT count(*) FROM ai_aiinvocation WHERE state='INTERRUPTED'").fetchone()[0] == 1
        assert database.execute("SELECT state,final_response FROM ai_aiinvocation WHERE id=?", (completed.pk.hex,)).fetchone() == ("SUCCEEDED", "완료된 과거 응답")
        assert database.execute("SELECT native_passed,file_tools_passed FROM ai_sandboxcheck").fetchone() == (0, 0)
        assert database.execute("SELECT validation_status FROM ai_modelprofile").fetchone()[0] == "UNVERIFIED"
        assert database.execute("SELECT readiness FROM projects_projecthealth").fetchone()[0] == "SETUP_REQUIRED"
        assert database.execute("SELECT job_id,owner FROM runs_workerslot").fetchone() == (None, "")
        assert database.execute("SELECT authz_version FROM accounts_user").fetchone()[0] == records.admin.authz_version + 1
        assert database.execute("SELECT state,draft_version,ready_revision_id FROM work_requests_workrequest").fetchone() == ("DRAFT", records.request.draft_version + 1, None)
        assert database.execute("SELECT count(*) FROM accounts_accountaudit WHERE action='operations.restore'").fetchone()[0] == 1
    assert (restored / "instance-id").read_text().strip() != runtime.instance_id
    assert not (restored / "restore-incomplete.json").exists()
    assert (restored / "restore-report.json").exists()
    assert hashlib.sha256((runtime.root / "db.sqlite3").read_bytes()).hexdigest() == source_checksum


def test_restore_marks_active_run_interrupted_after_confirming_old_process_exit(runtime, records, tmp_path, monkeypatch):
    records.run.status = "VERIFYING"
    records.run.save(update_fields=["status"])
    process = ProcessRecord.objects.create(job=records.job, run=records.run, stage="VERIFYING", worker_identity="old-worker", instance_id=runtime.instance_id, host_boot_id="boot", pid=55667, process_group_id=55667, process_start_identity="created")
    runtime.snapshot()
    monkeypatch.setattr(backup, "host_boot_id", lambda: "boot")
    monkeypatch.setattr(backup, "process_table", lambda: {})
    restored = backup.restore_backup(backup.create_backup(), tmp_path / "restored")
    with closing(sqlite3.connect(restored / "db.sqlite3")) as database:
        assert database.execute("SELECT status,error_code FROM runs_run").fetchone() == ("INTERRUPTED", "RESTORED_SNAPSHOT")
        assert database.execute("SELECT state,pid,instance_id FROM runs_processrecord WHERE id=?", (process.pk.hex,)).fetchone() == ("RESTORED", 55667, runtime.instance_id)
    assert (restored / "instance-id").read_text().strip() != runtime.instance_id


def test_restore_rebases_only_internal_dependency_storage(runtime, records, tmp_path):
    dependency_id = uuid.uuid4()
    folder = runtime.root / "dependencies" / str(dependency_id)
    folder.mkdir()
    (folder / "package.whl").write_bytes(b"wheel")
    DependencyBundle.objects.create(id=dependency_id, project=records.project, registered_path=str(folder), lock_file_hash="a" * 64, platform="test", runtime="python", manifest_sha256="b" * 64, manifest=[{"path": "package.whl", "size": 5, "sha256": hashlib.sha256(b"wheel").hexdigest()}], created_by=records.admin)
    runtime.snapshot()
    restored = backup.restore_backup(backup.create_backup(), tmp_path / "restored")
    with closing(sqlite3.connect(restored / "db.sqlite3")) as database:
        assert database.execute("SELECT registered_path FROM projects_dependencybundle").fetchone()[0] == str(restored / "dependencies" / str(dependency_id))
        assert database.execute("SELECT canonical_repo_path FROM projects_project").fetchone()[0] == records.project.canonical_repo_path


def test_restore_refuses_nonempty_destination_without_overwriting(runtime, tmp_path):
    runtime.snapshot()
    target = backup.create_backup()
    destination = tmp_path / "existing"
    destination.mkdir()
    file = destination / "original.txt"
    file.write_text("preserve")
    assert_error("RESTORE_DESTINATION_EXISTS", backup.restore_backup, target, destination)
    assert file.read_text() == "preserve"


def test_restore_refuses_live_source_after_old_snapshot(runtime, records, tmp_path, monkeypatch):
    runtime.snapshot()
    target = backup.create_backup()
    ProcessRecord.objects.create(job=records.job, stage="RUN", worker_identity="new-worker", instance_id=runtime.instance_id, host_boot_id="current-boot", pid=34567, process_group_id=34567, process_start_identity="start")
    runtime.snapshot()
    monkeypatch.setattr(backup, "host_boot_id", lambda: "current-boot")
    monkeypatch.setattr(backup, "process_table", lambda: {34567: (1, 34567, "start")})
    destination = tmp_path / "restored"
    assert_error("PROCESS_TERMINATION_UNCONFIRMED", backup.restore_backup, target, destination)
    assert not destination.exists()


def test_doctor_read_only_and_no_cli_from_dashboard(runtime, records, monkeypatch):
    runtime.snapshot()
    from integrations.codex import container

    monkeypatch.setattr(container, "check_connection", lambda *args, **kwargs: pytest.fail("dashboard must not execute CLI"))
    before = {model: model.objects.count() for model in (AIInvocation, Job, SandboxCheck, ModelProfile)}
    checks = doctor.inspect_local()
    assert any(row["id"] == "codex_login" and row["status"] == "NOT_RUN" for row in checks)
    assert before == {model: model.objects.count() for model in before}


def test_doctor_command_uses_only_status_probe_and_redacts_output(runtime, monkeypatch):
    runtime.snapshot()
    from integrations.codex import container

    calls = []

    def connection_status(*args, **kwargs):
        calls.append((args, kwargs))
        return {"connected": True, "supported_cli": True, "cli_version": "codex-cli 0.154.0", "raw": "SECRET_MUST_NOT_LEAK"}

    monkeypatch.setattr(container, "check_connection", connection_status)
    monkeypatch.setattr(container, "ready", lambda: "sha256:fixture")
    stream = StringIO()
    call_command("local_doctor", stdout=stream, as_json=True)
    payload = json.loads(stream.getvalue())
    assert calls == [((), {})]
    assert "SECRET_MUST_NOT_LEAK" not in stream.getvalue()
    assert {row["status"] for row in payload["checks"]}.issubset({"PASS", "WARN", "FAIL", "NOT_RUN"})


def test_doctor_reports_failure_without_leaking_backend_exception(runtime, monkeypatch):
    runtime.snapshot()
    from integrations.codex import container

    monkeypatch.setattr(container, "check_connection", lambda *args, **kwargs: {"connected": False, "error_code": "secret/raw/token"})
    stream = StringIO()
    with pytest.raises(CommandError):
        call_command("local_doctor", stdout=stream)
    assert "FAIL" in stream.getvalue()
    assert "secret/raw/token" not in stream.getvalue()


def test_doctor_uses_docker_status_without_model_execution(runtime, monkeypatch):
    runtime.snapshot()
    from integrations.codex import container

    calls = []
    monkeypatch.setattr(container, "ready", lambda: "sha256:fixture")

    def connection_status():
        calls.append("status")
        return {"connected": True, "supported_cli": True}

    monkeypatch.setattr(container, "check_connection", connection_status)
    monkeypatch.setattr(container, "launch", lambda **kwargs: pytest.fail("doctor must not invoke models"))
    checks = doctor.inspect_local(include_cli=True)
    assert calls == ["status"]
    assert any(row["id"] == "sandbox" and row["status"] == "PASS" for row in checks)


def test_operations_dashboard_requires_current_admin_role(client, runtime, records, monkeypatch):
    from integrations.codex import container

    monkeypatch.setattr(container, "check_connection", lambda *args, **kwargs: pytest.fail("GET must not probe CLI"))
    url = reverse("operations:dashboard")
    assert client.get(url).status_code == 302
    client.force_login(records.admin)
    session = client.session
    session["authz_version"] = records.admin.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()
    assert client.get(url).status_code == 200
    get_user_model().objects.filter(pk=records.admin.pk).update(role="MANAGER")
    assert client.get(url).status_code == 403
    assert client.post(url).status_code == 405
