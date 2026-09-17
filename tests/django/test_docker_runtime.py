import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.errors import DomainError
from integrations.codex.permissions import PermissionScope


def test_container_only_mounts_task_and_explicit_auth(tmp_path, monkeypatch):
    from integrations.codex import container

    monkeypatch.setattr(container, "docker_binary", lambda: "/fixture/docker")
    workspace = tmp_path / "task"
    workspace.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic")
    scope = PermissionScope(workspace, (workspace,))
    argv = container.run_argv("aiqa-test", scope, ["codex", "exec"], image="fixture:local", auth_file=auth)
    assert "bridge" == argv[argv.index("--network") + 1]
    assert "--privileged" not in argv
    assert not any("docker.sock" in arg for arg in argv)
    assert argv[argv.index("--workdir") + 1] == str(workspace)
    mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
    assert len(mounts) == 2
    assert any(f"src={workspace}," in mount for mount in mounts)
    assert any(f"src={auth}," in mount and "readonly" in mount for mount in mounts)


def test_container_command_keeps_model_and_effort_and_allows_web(tmp_path):
    from integrations.codex import container

    argv = container.codex_command({"model_id": "gpt-6-astra", "effort": "xhigh"}, tmp_path)
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="xhigh"' in argv
    assert 'web_search="live"' in argv
    assert "danger-full-access" in argv


def test_readonly_task_is_not_mounted_writable(tmp_path, monkeypatch):
    from integrations.codex import container

    monkeypatch.setattr(container, "docker_binary", lambda: "/fixture/docker")
    argv = container.run_argv("aiqa-test", PermissionScope(tmp_path, ()), ["true"], image="fixture:local")
    mount = argv[argv.index("--mount") + 1]
    assert mount.endswith(",readonly")


def test_missing_docker_does_not_fall_back_to_host(monkeypatch):
    from integrations.codex import container

    monkeypatch.setattr(container.shutil, "which", lambda value: None)
    with pytest.raises(DomainError) as error:
        container.docker_binary()
    assert error.value.code == "DOCKER_UNAVAILABLE"


@pytest.mark.django_db
def test_unconfirmed_container_is_not_reported_stopped(tmp_path, settings, monkeypatch):
    from apps.accounts.models import User
    from apps.runs.models import Job, ProcessRecord
    from integrations.codex import container

    user = User.objects.create_user("cleanup-test")
    job = Job.objects.create(kind="MODEL_VALIDATION", subject_id="test", actor=user,
                             instance_id=settings.AIQA_INSTANCE_ID, idempotency_key="cleanup", payload_hash="b" * 64)
    monkeypatch.setattr(container, "ready", lambda: "sha256:immutable")
    monkeypatch.setattr(container, "docker_binary", lambda: "/fixture/docker")
    def launch(**kwargs):
        assert "sha256:immutable" in kwargs["argv"]
        ProcessRecord.objects.create(job=job, stage=job.kind, container_name=kwargs["container_name"],
                                     worker_identity="worker", instance_id=settings.AIQA_INSTANCE_ID,
                                     host_boot_id="fixture", state="STOPPED")
        return SimpleNamespace(returncode=0)
    def remove(name):
        raise DomainError("CONTAINER_TERMINATION_UNCONFIRMED")
    monkeypatch.setattr(container, "remove", remove)
    with pytest.raises(DomainError) as error:
        container.launch(job=job, scope=PermissionScope(tmp_path, (tmp_path,)), command=["true"],
                         process_launcher=launch, timeout=5)
    assert error.value.code == "CONTAINER_TERMINATION_UNCONFIRMED"
    record = job.processes.get()
    assert record.state == "ABANDONED" and record.ended_at is None


@pytest.mark.parametrize("bundle_platform,bundle_runtime,accepted", [
    ("linux-aarch64", "python-3.12", True),
    ("darwin-arm64", "python-3.11", False),
])
def test_dependency_bundle_matches_container_not_host(tmp_path, monkeypatch, bundle_platform, bundle_runtime, accepted):
    import hashlib
    from apps.projects import services
    from apps.projects.models import DependencyBundle
    from apps.runs.pipeline import _copy_dependencies
    from integrations.codex import container

    lock = b"fixture lock\n"
    (tmp_path / "requirements.lock").write_bytes(lock)
    bundle = SimpleNamespace(pk="bundle", platform=bundle_platform, runtime=bundle_runtime,
                             lock_file_hash=hashlib.sha256(lock).hexdigest(), registered_path=str(tmp_path), manifest=[])
    monkeypatch.setattr(DependencyBundle.objects, "get", lambda **kwargs: bundle)
    monkeypatch.setattr(services, "verify_dependency_bundle", lambda value: value)
    monkeypatch.setattr(container, "runtime_info", lambda: {"system": "Linux", "machine": "aarch64", "runtime": "python-3.12"})
    policy = {"preparation_commands": [{"dependency_bundle_id": "bundle"}]}
    if accepted:
        _copy_dependencies(tmp_path, policy)
        assert (tmp_path / ".aiqa-inputs/bundle").is_dir()
    else:
        with pytest.raises(DomainError) as error:
            _copy_dependencies(tmp_path, policy)
        assert error.value.code == "DEPENDENCY_MISSING"


@pytest.mark.django_db
def test_unconfirmed_local_job_can_release_slot_without_claiming_process_stopped(settings):
    from apps.accounts.models import User
    from apps.runs.models import Job, ProcessRecord, WorkerSlot
    from apps.runs.worker import abandon_local_processes

    user = User.objects.create_user("docker-retry")
    job = Job.objects.create(kind="MODEL_VALIDATION", subject_id="test", actor=user,
                             instance_id=settings.AIQA_INSTANCE_ID, idempotency_key="retry", payload_hash="a" * 64)
    WorkerSlot.objects.create(name=job.slot, job=job, blocked_reason="EXECUTION_BLOCKED")
    record = ProcessRecord.objects.create(job=job, stage=job.kind, worker_identity="old",
                                          instance_id=settings.AIQA_INSTANCE_ID, host_boot_id="old")
    assert abandon_local_processes(job)
    record.refresh_from_db()
    assert record.state == "ABANDONED"
    assert record.ended_at is None
    assert WorkerSlot.objects.get(pk=job.slot).blocked_reason == ""
    job.kind = "publication.push"
    assert not abandon_local_processes(job)


@pytest.mark.skipif(os.environ.get("AIQA_RUN_DOCKER_TESTS") != "1", reason="Explicit Docker integration run")
def test_real_container_network_and_workspace_boundary(tmp_path):
    from integrations.codex import container

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "host-only.txt"
    outside.write_text("synthetic host file")
    scope = PermissionScope(workspace, (workspace,))
    name = "aiqa-" + uuid4().hex
    command = ["python3", "-c", "import pathlib, urllib.request; "
               f"assert not pathlib.Path({str(outside)!r}).exists(); "
               "assert urllib.request.urlopen('https://pypi.org/simple/', timeout=15).status == 200; "
               "pathlib.Path('result.txt').write_text('network and workspace OK')"]
    try:
        result = subprocess.run(container.run_argv(name, scope, command, image=container.image_name()),
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert (workspace / "result.txt").read_text() == "network and workspace OK"
        assert outside.read_text() == "synthetic host file"
    finally:
        container.remove(name)


@pytest.mark.skipif(os.environ.get("AIQA_RUN_CODEX_TESTS") != "1", reason="Explicit real Codex usage")
def test_real_codex_modifies_separate_worktree_and_docker_verifies(tmp_path, settings):
    from integrations.codex import adapter, container
    from apps.runs.workspace import create_workspace, freeze_candidate
    from tests.django.test_workspace_local import git, repository_fingerprint

    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "--template=", "--initial-branch=main")
    (repo / "answer.py").write_text("ANSWER = 41\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Synthetic fixture")
    before = repository_fingerprint(repo)
    settings.AIQA_DATA_DIR = tmp_path / "runtime"
    workspace = create_workspace(repo, git(repo, "rev-parse", "HEAD").decode().strip(), uuid4(), {}, settings.AIQA_DATA_DIR)
    scope = PermissionScope(workspace.workspace, (workspace.workspace,))
    name = "aiqa-" + uuid4().hex
    profile = {"model_id": "gpt-6-astra", "effort": "xhigh"}
    try:
        result = subprocess.run(container.run_argv(name, scope, container.codex_command(profile, scope.workspace),
                                image=container.image_name(), auth_file=container.login_file()),
                                input="In this synthetic project, change only answer.py to exactly ANSWER = 42 followed by a newline. Do not commit. Reply DONE.",
                                text=True, capture_output=True, timeout=180)
        parsed = adapter.parse_jsonl(result.stdout, returncode=result.returncode,
                                    requested_model=profile["model_id"], requested_effort=profile["effort"])
        assert not parsed.error_code, parsed.error_code
        assert (workspace.workspace / "answer.py").read_text() == "ANSWER = 42\n"
    finally:
        container.remove(name)
    frozen = freeze_candidate(workspace, {})
    assert frozen.changed_paths == ("answer.py",)
    name = "aiqa-" + uuid4().hex
    try:
        result = subprocess.run(container.run_argv(name, PermissionScope(frozen.after_checks, ()),
                                ["python3", "-c", "from answer import ANSWER; assert ANSWER == 42; print('PASS')"],
                                image=container.image_name()), capture_output=True, text=True, timeout=30)
        assert result.returncode == 0 and result.stdout.strip() == "PASS"
    finally:
        container.remove(name)
    assert repository_fingerprint(repo) == before
