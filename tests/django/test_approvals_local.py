import uuid
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.approvals import services
from apps.approvals.models import Approval, Publication, PublicationAction, PublicationAttempt
from apps.approvals.transport import GitHubTransport, RemoteObservation, new_ref_push_args, public_summary
from apps.artifacts.models import ArtifactPin, ResultBundle
from apps.conversations.models import Conversation
from apps.errors import DomainError
from apps.projects.models import Project, ProjectConfigRevision, VerificationCommand
from apps.runs.models import CheckResult, Job, Run
from apps.work_requests.models import RequestRevision, WorkRequest


pytestmark = pytest.mark.django_db


class FakeRemote:
    def __init__(self):
        self.base_sha = "a" * 40
        self.branch_sha = ""
        self.pr_number = None
        self.pushes = 0
        self.pr_creations = 0
        self.push_error = False
        self.pr_error = False
        self.unavailable = False
        self.after_push = None

    def prepare(self, snapshot, bundle, job):
        return "b" * 40

    def observe(self, snapshot, job):
        if self.unavailable:
            raise DomainError("REMOTE_COMMAND_FAILED")
        return RemoteObservation("123", "example/sample", self.base_sha, self.branch_sha, self.pr_number,
                                 f"https://github.com/example/sample/pull/{self.pr_number}" if self.pr_number else "",
                                 self.branch_sha if self.pr_number else "", bool(self.pr_number))

    def push(self, snapshot, job):
        assert not self.branch_sha
        self.pushes += 1
        self.branch_sha = snapshot["commit_sha"]
        if self.after_push:
            self.after_push()
        if self.push_error:
            raise TimeoutError("fixture response lost")

    def create_pr(self, snapshot, job):
        self.pr_creations += 1
        if self.pr_error:
            raise TimeoutError("fixture response lost")
        self.pr_number = 42


@pytest.fixture
def setup(monkeypatch, settings, tmp_path):
    settings.AIQA_DATA_DIR = tmp_path
    User = get_user_model()
    admin = User.objects.create_user(username="publisher", password="Temporary-only-935!", role="ADMIN", display_name="관리자")
    other_admin = User.objects.create_user(username="reviewer", password="Temporary-only-935!", role="ADMIN", display_name="검토자")
    manager = User.objects.create_user(username="manager", password="Temporary-only-935!", role="MANAGER", display_name="매니저")
    project = Project.objects.create(name="Sample", canonical_repo_path=str(tmp_path / "source"), created_by=admin)
    config = ProjectConfigRevision.objects.create(project=project, project_config_version=1, canonical_repo_path=project.canonical_repo_path, source_ref="main", created_by=admin, read_paths=["."], write_paths=["."])
    VerificationCommand.objects.create(config_revision=config, command_id="tests", display_name="Tests", kind="TEST", argv=["pytest"], parser_id="pytest", parser_version=1)
    conversation = Conversation.objects.create(project=project, owner=manager, title="Fix", creation_key="first", creation_hash="e" * 64)
    request = WorkRequest.objects.create(project=project, owner=manager, conversation=conversation)
    revision = RequestRevision.objects.create(request=request, number=1, source_draft_version=1, snapshot={"title": "Fix"}, content_hash="c" * 64, confirmed_by=manager, project_config_version=1, base_commit_sha="a" * 40)
    run = Run.objects.create(project=project, request_revision=revision, requested_by=manager, status="SUCCEEDED", baseline_commit_sha="a" * 40, project_config_version=1, model_profile_version=1, idempotency_key="run")
    now = timezone.now()
    bundle = ResultBundle.objects.create(run=run, request_revision=revision, version=1, baseline_commit_sha=run.baseline_commit_sha, project_config_version=1, manifest_sha256="c" * 64, result_tree_sha256="d" * 64, completeness="COMPLETE", visibility="NORMAL", sealed_at=now, expires_at=now + timedelta(days=30))
    run.result_bundle_id = bundle.id
    run.save(update_fields=["result_bundle_id"])
    from apps.artifacts.models import ResultReview
    ResultReview.objects.create(bundle=bundle, manifest_sha256=bundle.manifest_sha256, state="APPROVED", accepted_by=manager, decided_by=admin)
    CheckResult.objects.create(run=run, phase="AFTER", check_id="tests", config_version=1, argv=["pytest"], started_at=now, finished_at=now, exit_code=0, checked_source_tree_sha256=bundle.result_tree_sha256, outcome="PASS", collected_count=1, passed_count=1, failed_count=0, skipped_count=0, output_complete=True)

    def verify(bundle_id, expected_hash):
        current = ResultBundle.objects.get(pk=bundle_id)
        if current.manifest_sha256 != expected_hash:
            raise DomainError("ARTIFACT_CORRUPTED")
        return current

    monkeypatch.setattr("apps.artifacts.services.verify_bundle", verify)
    monkeypatch.setattr("apps.ai.services.require_sandbox_ready", lambda *a, **kw: True)
    monkeypatch.setattr("apps.projects.services.validate_repository_path", lambda value: value)
    monkeypatch.setattr("apps.projects.services.resolve_base_sha", lambda *args: "a" * 40)
    values = dict(enabled=True, repository_host="github.com", repository_id="123", repository_full_name="example/sample", remote_url="https://github.com/example/sample.git", base_branch="main", mode="PUSH_AND_PR", commit_author_name="Local Admin", commit_author_email="admin@example.com")
    policy = services.configure_policy(admin, project.id, 0, **values)
    return SimpleNamespace(admin=admin, other_admin=other_admin, manager=manager, project=project, run=run, bundle=bundle, policy=policy, policy_values=values, remote=FakeRemote())


def process(job, remote):
    job.state = "RUNNING"
    job.save(update_fields=["state"])
    result = services.process_job(job, transport=remote)
    job.state = "DONE"
    job.save(update_fields=["state"])
    return result


def prepared(setup, key="prepare"):
    job = services.prepare_publication(setup.admin, setup.run.id, key)
    return process(job, setup.remote)


def approved(setup):
    publication = prepared(setup)
    services.decide_publication(setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "approve")
    return publication, Job.objects.get(kind="publication.execute")


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def test_manager_cannot_prepare_approve_reject_or_create_jobs(setup):
    assert_error("ACCESS_DENIED", services.prepare_publication, setup.manager, setup.run.id, "manager")
    assert Job.objects.count() == 0
    publication = prepared(setup)
    count = Job.objects.count()
    for decision in ("APPROVE", "REJECT"):
        assert_error("ACCESS_DENIED", services.decide_publication, setup.manager, publication.id, publication.snapshot_hash, decision, "manager")
    assert Job.objects.count() == count
    assert not Approval.objects.exists()


def test_atomic_approval_idempotence_and_conflict(setup):
    publication = prepared(setup)
    first = services.decide_publication(setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "approve")
    again = services.decide_publication(setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "approve")
    assert first.id == again.id
    assert Approval.objects.count() == 1
    assert Job.objects.filter(kind="publication.execute").count() == 1
    assert_error("IDEMPOTENCY_CONFLICT", services.decide_publication, setup.admin, publication.id, publication.snapshot_hash, "REJECT", "approve")
    assert_error("STATE_CONFLICT", services.decide_publication, setup.other_admin, publication.id, publication.snapshot_hash, "REJECT", "other")


def test_approval_and_state_roll_back_when_enqueue_fails(setup, monkeypatch):
    publication = prepared(setup)

    def fail(*args, **kwargs):
        raise DomainError("QUEUE_UNAVAILABLE")

    monkeypatch.setattr("apps.runs.services.enqueue_job", fail)
    assert_error("QUEUE_UNAVAILABLE", services.decide_publication, setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "broken")
    publication.refresh_from_db()
    assert publication.state == "PENDING_APPROVAL"
    assert not Approval.objects.exists()
    assert not PublicationAction.objects.filter(key="broken").exists()


def test_successful_push_and_draft_pr_are_separate_from_run(setup):
    publication, job = approved(setup)
    publication = process(job, setup.remote)
    assert publication.state == "PUBLISHED"
    assert publication.remote_pr_number == 42
    assert setup.remote.pushes == 1 and setup.remote.pr_creations == 1
    assert not ArtifactPin.objects.filter(bundle=setup.bundle).exists()
    setup.run.refresh_from_db()
    assert setup.run.status == "SUCCEEDED"
    assert PublicationAttempt.objects.count() == 2


def test_push_only_does_not_create_pr(setup):
    setup.policy_values["mode"] = "PUSH"
    services.configure_policy(setup.admin, setup.project.id, 1, **setup.policy_values)
    publication, job = approved(setup)
    assert process(job, setup.remote).state == "PUBLISHED"
    assert setup.remote.pushes == 1 and setup.remote.pr_creations == 0


@pytest.mark.parametrize("changes", [
    {"outcome": "UNVERIFIED"}, {"passed_count": 0}, {"output_complete": False}, {"exit_code": 1}, {"checked_source_tree_sha256": "0" * 64},
])
def test_incomplete_checks_block_preparation(setup, changes):
    CheckResult.objects.filter(run=setup.run).update(**changes)
    assert_error("CHECKS_INCOMPLETE", services.prepare_publication, setup.admin, setup.run.id, "prepare")
    assert not Job.objects.exists()


def test_required_check_absence_and_restricted_result_are_not_publishable(setup):
    setup.bundle.visibility = "RESTRICTED"
    setup.bundle.save(update_fields=["visibility"])
    assert_error("RESULT_NOT_PUBLISHABLE", services.prepare_publication, setup.admin, setup.run.id, "prepare")


def test_native_safety_is_not_bypassed_by_admin_or_policy(setup, monkeypatch):
    def deny(*args, **kwargs):
        raise DomainError("SANDBOX_UNAVAILABLE")

    monkeypatch.setattr("apps.ai.services.require_sandbox_ready", deny)
    assert_error("SANDBOX_UNAVAILABLE", services.prepare_publication, setup.admin, setup.run.id, "prepare")
    assert not Job.objects.exists()


def test_initial_enable_and_policy_changes_do_not_change_execution_version(setup):
    publication = prepared(setup)
    setup.policy_values["mode"] = "PUSH"
    services.configure_policy(setup.admin, setup.project.id, 1, **setup.policy_values)
    publication.refresh_from_db()
    setup.project.refresh_from_db()
    assert setup.project.project_config_version == 1
    assert publication.state == "INVALIDATED"
    fresh = prepared(setup, "prepare-new-policy")
    assert fresh.id != publication.id and fresh.snapshot["policy"]["version"] == 2


def test_policy_change_refused_while_publication_job_queued(setup):
    prepared(setup)
    publication = Publication.objects.get()
    services.decide_publication(setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "approve")
    assert_error("PROJECT_BUSY", services.configure_policy, setup.admin, setup.project.id, 1, **setup.policy_values)


def test_approval_fails_for_changed_result_or_snapshot(setup):
    publication = prepared(setup)
    assert_error("SNAPSHOT_CHANGED", services.decide_publication, setup.admin, publication.id, "0" * 64, "APPROVE", "stale")
    ResultBundle.objects.filter(pk=setup.bundle.id).update(manifest_sha256="f" * 64)
    assert_error("ARTIFACT_CORRUPTED", services.decide_publication, setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "changed")
    assert not Approval.objects.exists()


def test_revoked_approver_is_checked_before_remote_write(setup):
    publication, job = approved(setup)
    setup.admin.is_active = False
    setup.admin.save(update_fields=["is_active"])
    result = process(job, setup.remote)
    assert result.state == "INVALIDATED"
    assert setup.remote.pushes == setup.remote.pr_creations == 0


def test_base_move_before_write_invalidates_without_push(setup):
    publication, job = approved(setup)
    setup.remote.base_sha = "f" * 40
    assert process(job, setup.remote).state == "INVALIDATED"
    assert setup.remote.pushes == 0


def test_local_source_ref_change_invalidates_approval(setup, monkeypatch):
    publication = prepared(setup)
    monkeypatch.setattr("apps.projects.services.resolve_base_sha", lambda *args: "f" * 40)
    assert_error("BASE_CHANGED", services.decide_publication, setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "changed-source")
    assert not Approval.objects.exists()


def test_base_move_after_push_keeps_actual_remote_effect(setup):
    publication, job = approved(setup)
    setup.remote.after_push = lambda: setattr(setup.remote, "base_sha", "f" * 40)
    result = process(job, setup.remote)
    assert result.state == "PARTIAL"
    assert result.remote_branch_sha == "b" * 40
    assert setup.remote.pr_creations == 0
    job = services.reconcile_publication(setup.admin, result.id, "reconcile")
    result = process(job, setup.remote)
    assert result.state == "PARTIAL"
    assert ArtifactPin.objects.filter(bundle=setup.bundle).exists()


def test_lost_push_response_requires_read_reconciliation_and_never_repushes(setup):
    publication, job = approved(setup)
    setup.remote.push_error = True
    result = process(job, setup.remote)
    assert result.state == "UNKNOWN"
    assert_error("RECOVERY_NOT_READY", services.approve_remaining, setup.admin, result.id, result.snapshot_hash, "premature")
    result = process(services.reconcile_publication(setup.admin, result.id, "reconcile"), setup.remote)
    assert result.state == "PARTIAL"
    setup.remote.push_error = False
    services.approve_remaining(setup.admin, result.id, result.snapshot_hash, "continue")
    result = process(Job.objects.filter(kind="publication.execute", state="QUEUED").get(), setup.remote)
    assert result.state == "PUBLISHED"
    assert setup.remote.pushes == 1


def test_new_admin_can_approve_only_remaining_pr_after_revocation(setup):
    publication, job = approved(setup)

    def revoke():
        setup.admin.is_active = False
        setup.admin.save(update_fields=["is_active"])

    setup.remote.after_push = revoke
    result = process(job, setup.remote)
    assert result.state == "PARTIAL"
    result = process(services.reconcile_publication(setup.other_admin, result.id, "reconcile"), setup.remote)
    services.approve_remaining(setup.other_admin, result.id, result.snapshot_hash, "new-admin")
    result = process(Job.objects.filter(kind="publication.execute", state="QUEUED").get(), setup.remote)
    assert result.state == "PUBLISHED" and setup.remote.pushes == 1
    approvals = list(Approval.objects.order_by("decided_at"))
    assert approvals[0].actor_id == setup.admin.id and not approvals[0].valid
    assert approvals[1].actor_id == setup.other_admin.id and approvals[1].scope == "REMAINING_PR"


def test_unknown_cannot_be_abandoned_or_unpinned(setup):
    publication, job = approved(setup)
    setup.remote.push_error = True
    result = process(job, setup.remote)
    assert_error("RECOVERY_NOT_READY", services.abandon_remaining, setup.admin, result.id, result.snapshot_hash, "Stop", "abandon")
    assert ArtifactPin.objects.filter(bundle=setup.bundle).exists()
    result = process(services.reconcile_publication(setup.admin, result.id, "reconcile"), setup.remote)
    result = services.abandon_remaining(setup.admin, result.id, result.snapshot_hash, "Do not create PR", "abandon")
    assert result.state == "PARTIAL" and result.abandoned_at
    assert not ArtifactPin.objects.filter(bundle=setup.bundle).exists()
    assert_error("RECOVERY_NOT_READY", services.approve_remaining, setup.admin, result.id, result.snapshot_hash, "again")


def test_remote_query_error_is_not_absence(setup):
    publication, job = approved(setup)
    setup.remote.push_error = True
    result = process(job, setup.remote)
    setup.remote.unavailable = True
    result = process(services.reconcile_publication(setup.admin, result.id, "reconcile"), setup.remote)
    assert result.state == "UNKNOWN" and not result.reconciled_at


def test_previous_observed_remote_changes_are_not_erased_when_ref_disappears(setup):
    publication, job = approved(setup)
    setup.remote.pr_error = True
    result = process(job, setup.remote)
    assert result.remote_effects_observed
    setup.remote.branch_sha = ""
    result = process(services.reconcile_publication(setup.admin, result.id, "reconcile"), setup.remote)
    assert result.state == "PARTIAL" and result.remote_branch_sha == "b" * 40
    assert_error("RECOVERY_NOT_READY", services.approve_remaining, setup.admin, result.id, result.snapshot_hash, "missing-branch")


def test_decision_and_snapshot_records_cannot_be_rewritten(setup):
    publication, job = approved(setup)
    approval = Approval.objects.get()
    approval.actor = setup.other_admin
    with pytest.raises(ValidationError):
        approval.save()
    with pytest.raises(ValidationError):
        Approval.objects.filter(pk=approval.pk).update(decision="REJECT")
    publication.snapshot = {**publication.snapshot, "commit_sha": "e" * 40}
    with pytest.raises(ValidationError):
        publication.save()


@pytest.mark.parametrize("remote", ["ssh://github.com/example/sample", "https://secret@github.com/example/sample.git", "https://evil.test/example/sample.git", "https://github.com/example/other.git"])
def test_arbitrary_or_credential_urls_are_rejected(setup, remote):
    setup.policy_values["remote_url"] = remote
    assert_error("INVALID_PUBLICATION_TARGET", services.configure_policy, setup.admin, setup.project.id, 1, **setup.policy_values)


def test_new_ref_push_has_exact_absent_ref_lease_and_one_refspec():
    branch = f"aiqa/run-{uuid.uuid4()}"
    arguments = new_ref_push_args("https://github.com/example/sample.git", branch, "b" * 40)
    assert f"--force-with-lease=refs/heads/{branch}:" in arguments
    assert arguments[-1] == f"{'b' * 40}:refs/heads/{branch}"
    assert "--force" not in arguments
    assert not any(argument.startswith("+") for argument in arguments)


def test_transport_uses_fixed_clean_environment_without_inherited_tokens(monkeypatch, tmp_path):
    transport = object.__new__(GitHubTransport)
    transport.auth_dir = tmp_path / "host-gh-config"
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-inherit")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "9")
    monkeypatch.setenv("SSH_AUTH_SOCK", "must-not-inherit")
    env = transport._environment(tmp_path)
    assert not {"GITHUB_TOKEN", "GIT_CONFIG_COUNT", "SSH_AUTH_SOCK"} & env.keys()
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_TERMINAL_PROMPT"] == "0"


def test_manager_post_and_csrf_get_do_not_create_approvals(setup):
    publication = prepared(setup)
    client = Client(enforce_csrf_checks=True)
    client.force_login(setup.manager)
    session = client.session
    session["authz_version"] = setup.manager.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()
    path = f"/approvals/{publication.id}/approve/"
    assert client.get(path).status_code == 405
    response = client.post(path, {"snapshot_hash": publication.snapshot_hash, "idempotency_key": "manager"})
    assert response.status_code == 403
    assert not Approval.objects.exists()


def _local_git(path, *args, env=None):
    result = subprocess.run([shutil.which("git"), *args], cwd=path, env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def test_atomic_new_ref_lease_rejects_competing_local_ref(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    remote = tmp_path / "remote.git"
    _local_git(tmp_path, "init", "--bare", "--quiet", str(remote))
    _local_git(source, "init", "--quiet")
    (source / "file.txt").write_text("first\n")
    _local_git(source, "add", "file.txt")
    _local_git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "first")
    first = _local_git(source, "rev-parse", "HEAD").decode().strip()
    (source / "file.txt").write_text("second\n")
    _local_git(source, "add", "file.txt")
    _local_git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "second")
    second = _local_git(source, "rev-parse", "HEAD").decode().strip()
    branch = f"aiqa/run-{uuid.uuid4()}"
    args = new_ref_push_args("https://github.com/example/sample.git", branch, first)
    args[-2] = str(remote)
    _local_git(source, *args)
    args[-1] = f"{second}:refs/heads/{branch}"
    raced = subprocess.run([shutil.which("git"), *args], cwd=source, capture_output=True, timeout=10)
    assert raced.returncode != 0
    assert _local_git(remote, "rev-parse", f"refs/heads/{branch}").decode().strip() == first


def test_trusted_assembly_uses_remote_parent_and_plain_data_without_filters(setup, tmp_path, monkeypatch):
    from apps.artifacts.trees import tree_digest

    baseline = {"app.py": b"print('before')\n", ".gitattributes": b"app.py filter=untrusted\n"}
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    for path, data in baseline.items():
        (fixture / path).write_bytes(data)
    _local_git(fixture, "init", "--quiet", "--initial-branch=main")
    _local_git(fixture, "add", "--all")
    _local_git(fixture, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "baseline")
    baseline_sha = _local_git(fixture, "rev-parse", "HEAD").decode().strip()
    (fixture / "app.py").write_text("print('after')\n")
    canonical_patch = _local_git(fixture, "diff", "--binary", "HEAD")
    expected_tree = tree_digest(fixture)
    marker = tmp_path / "MUST_NOT_RUN"
    malicious = tmp_path / "malicious.gitconfig"
    malicious.write_text(f'[filter "untrusted"]\n clean = touch {marker}\n smudge = touch {marker}\n[core]\n hooksPath = {tmp_path}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(malicious))
    transport = object.__new__(GitHubTransport)
    transport.git = shutil.which("git")
    transport.gh = "/usr/bin/false"
    transport.auth_dir = tmp_path / "gh-unused"

    def materialize(bundle_id, destination):
        destination.mkdir()
        for name, data in baseline.items():
            (destination / name).write_bytes(data)
        return destination

    monkeypatch.setattr("apps.artifacts.services.materialize_baseline", materialize)
    monkeypatch.setattr("apps.artifacts.services.read_bundle_file", lambda bundle_id, path: canonical_patch)

    def local_process(job, directory, args, *, stdin=None, env=None, allow_absent=False):
        assert args[0] == transport.git
        command = list(args)
        if "fetch" in command:
            command = [str(fixture) if part == "https://github.com/example/sample.git" else "protocol.file.allow=always" if part == "protocol.file.allow=never" else part for part in command]
        result = subprocess.run(command, cwd=directory, env=env or transport._environment(directory), input=stdin.encode() if stdin else None, capture_output=True, timeout=10)
        if result.returncode:
            raise AssertionError(result.stderr.decode())
        return result.stdout.decode()

    monkeypatch.setattr(transport, "_run", local_process)
    policy = services._policy_data(setup.policy)
    snapshot = {"baseline_commit_sha": baseline_sha, "result_tree_sha256": expected_tree, "policy": policy, "commit_timestamp": "2026-09-11T00:00:00+00:00", "commit_message": "Approved fixture result"}
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    source, commit = transport._assemble(snapshot, setup.bundle, None, first_dir)
    _, repeated = transport._assemble(snapshot, setup.bundle, None, second_dir)
    assert commit == repeated
    assert _local_git(source, "rev-parse", f"{commit}^").decode().strip() == baseline_sha
    assert _local_git(source, "show", f"{commit}:app.py") == b"print('after')\n"
    assert not marker.exists()


def test_current_role_filters_publication_list_even_with_stale_admin_object(setup):
    from apps.accounts.models import ProjectMembership
    from apps.approvals.views import list_view

    prepared(setup)
    stale_admin = setup.admin
    get_user_model().objects.filter(pk=stale_admin.pk).update(role="MANAGER")
    ProjectMembership.objects.create(project=setup.project, user=stale_admin, granted_by=setup.other_admin)
    request = RequestFactory().get("/approvals/")
    request.user = stale_admin
    response = list_view(request)
    assert response.status_code == 200
    assert "example/sample" not in response.content.decode()


def test_demotion_then_promotion_does_not_revive_original_approval(setup):
    from apps.accounts.models import ProjectMembership
    from apps.accounts.services import change_account

    publication, job = approved(setup)
    ProjectMembership.objects.create(project=setup.project, user=setup.admin, granted_by=setup.other_admin)
    changed = change_account(setup.other_admin, setup.admin.pk, setup.admin.authz_version, {"role": "MANAGER"}, reason="Remove publisher privilege")
    change_account(setup.other_admin, changed.pk, changed.authz_version, {"role": "ADMIN"}, reason="Grant privilege again")
    original = Approval.objects.get()
    assert not original.valid
    publication.refresh_from_db()
    assert publication.state == "INVALIDATED"
    job.state = "RUNNING"
    job.save(update_fields=["state"])
    assert_error("APPROVAL_INVALID", services.process_job, job, setup.remote)
    assert setup.remote.pushes == setup.remote.pr_creations == 0


def test_worker_crash_marks_started_publication_unknown_not_resumable(setup):
    publication, job = approved(setup)
    Publication.objects.filter(pk=publication.pk).update(state="PUBLISHING")
    services.mark_job_interrupted(job, "WORKER_LOST")
    publication.refresh_from_db()
    assert publication.state == "UNKNOWN"
    assert not Approval.objects.get().valid
    assert ArtifactPin.objects.filter(bundle=setup.bundle).exists()


def test_public_summary_strips_local_paths_but_keeps_public_urls():
    summary = public_summary("파일 /Users/manager/work/app.py 수정. 링크 https://github.com/example/sample C:\\Users\\manager\\app.py")
    assert "/Users/manager" not in summary and "C:\\Users" not in summary
    assert "https://github.com/example/sample" in summary


@pytest.mark.django_db(transaction=True)
def test_concurrent_opposite_decisions_never_commit_two_approvals(setup):
    publication = prepared(setup)
    barrier = Barrier(2)

    def decide(data):
        close_old_connections()
        actor_id, decision = data
        try:
            actor = get_user_model().objects.get(pk=actor_id)
            barrier.wait(timeout=10)
            try:
                services.decide_publication(actor, publication.id, publication.snapshot_hash, decision, f"race-{decision}")
                return "COMMITTED"
            except DomainError as error:
                return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, [(setup.admin.id, "APPROVE"), (setup.other_admin.id, "REJECT")]))
    assert results.count("COMMITTED") <= 1
    assert set(results) <= {"COMMITTED", "STATE_CONFLICT"}
    if not Approval.objects.exists():
        services.decide_publication(setup.admin, publication.id, publication.snapshot_hash, "APPROVE", "race-APPROVE")
    approval = Approval.objects.get()
    assert Job.objects.filter(kind="publication.execute").count() == (1 if approval.decision == "APPROVE" else 0)


def _logged_in(user):
    client = Client()
    client.force_login(user)
    session = client.session
    session["authz_version"] = user.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()
    return client


def test_preparation_progress_is_read_only_and_links_to_prepared_result(setup):
    client = _logged_in(setup.admin)
    job = services.prepare_publication(setup.admin, setup.run.id, "prepare")
    response = client.get(f"/approvals/jobs/{job.id}/")
    assert response.status_code == 200
    assert Job.objects.count() == 1
    publication = process(job, setup.remote)
    response = client.get(f"/approvals/jobs/{job.id}/")
    assert response.status_code == 302 and response.url == f"/approvals/{publication.id}/"
    assert Job.objects.count() == 1


def test_authenticated_manager_direct_post_has_no_approval_side_effect(setup):
    publication = prepared(setup)
    client = _logged_in(setup.manager)
    response = client.post(f"/approvals/{publication.id}/approve/", {"snapshot_hash": publication.snapshot_hash, "idempotency_key": "direct"})
    assert response.status_code == 403
    assert not Approval.objects.exists()


def test_failed_preparation_displays_error_without_secrets(setup):
    client = _logged_in(setup.admin)
    job = services.prepare_publication(setup.admin, setup.run.id, "prepare")
    job.state = "FAILED"
    job.error_code = "PUBLICATION_TOOL_MISSING"
    job.save(update_fields=["state", "error_code"])
    response = client.get(f"/approvals/jobs/{job.id}/")
    assert response.status_code == 200
    assert "PUBLICATION_TOOL_MISSING" in response.content.decode()
    assert "발행 준비 다시 요청" in response.content.decode()
