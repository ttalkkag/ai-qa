import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from apps.runs.workspace import (
    WorkspaceError,
    create_workspace,
    create_readonly_workspace,
    commit_candidate,
    freeze_candidate,
    source_tree_sha256,
    tree_manifest,
    tree_sha256,
)


def git(repo, *arguments, input_data=None):
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@localhost", "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@localhost",
    }
    return subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
        input=input_data, capture_output=True, check=True, env=environment,
    ).stdout


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "--template=", "--initial-branch=main")
    (repo / "src").mkdir()
    (repo / "src/app.py").write_bytes(b"answer = 1\n")
    (repo / "readme.md").write_bytes(b"committed documentation\n")
    (repo / "private.txt").write_bytes(b"hidden source\n")
    (repo / ".env").write_bytes(b"SECRET=do-not-project\n")
    (repo / ".codex").mkdir()
    (repo / ".codex/config.toml").write_bytes(b"malicious_source_config = true\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Fixture")
    return repo


def workspace_for(repository, *, policy=None):
    return create_workspace(
        repository, git(repository, "rev-parse", "HEAD").decode().strip(), uuid4(),
        policy or {"read_paths": ["."], "write_paths": ["."], "excluded_paths": ["private.txt", ".git/"]},
        repository.parent / "runtime",
    )


def repository_fingerprint(repo):
    return {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in repo.rglob("*") if path.is_file() and not path.is_symlink()}


def test_snapshot_uses_committed_tree_preserving_dirty_index_refs_and_independence(repository):
    (repository / "src/app.py").write_bytes(b"staged = 2\n")
    git(repository, "add", "src/app.py")
    (repository / "src/app.py").write_bytes(b"dirty = 3\n")
    (repository / "untracked.txt").write_bytes(b"local only\n")
    before = repository_fingerprint(repository)
    workspace = workspace_for(repository)
    assert repository_fingerprint(repository) == before
    assert (workspace.workspace / "src/app.py").read_bytes() == b"answer = 1\n"
    assert not (workspace.workspace / "untracked.txt").exists()
    assert not (workspace.workspace / ".env").exists()
    assert not (workspace.workspace / ".codex").exists()
    assert not (workspace.workspace / "private.txt").exists()
    assert (workspace.full_baseline / "private.txt").read_bytes() == b"hidden source\n"
    assert workspace.snapshot_base_commit_sha != git(repository, "rev-parse", "HEAD").decode().strip()
    assert git(workspace.workspace, "rev-parse", "HEAD").decode().strip() == workspace.snapshot_base_commit_sha
    assert git(workspace.workspace, "remote").strip() == b""
    assert not (workspace.workspace / ".git/objects/info/alternates").exists()
    assert (workspace.workspace / "src/app.py").stat().st_ino != (repository / "src/app.py").stat().st_ino
    assert (workspace.workspace / ".git").is_file()
    assert str(workspace.workspace) in git(workspace.workspace, "worktree", "list").decode()
    assert git(workspace.workspace, "rev-parse", "--git-common-dir").decode().strip() != str(repository / ".git")
    assert git(workspace.workspace, "branch", "--show-current").decode().strip() == f"temp/aiqa-{workspace.workspace.name}"


def test_conversation_snapshot_contains_only_allowed_committed_projection(repository):
    runtime = repository.parent / "runtime"
    invocation_id = uuid4()
    before = repository_fingerprint(repository)
    path = create_readonly_workspace(repository, git(repository, "rev-parse", "HEAD").decode().strip(), invocation_id,
                                     {"read_paths": ["src"], "write_paths": [], "excluded_paths": []}, runtime)
    assert path == runtime / "conversations-tmp" / str(invocation_id)
    assert (path / "src/app.py").read_bytes() == b"answer = 1\n"
    assert not (path / "private.txt").exists()
    assert not (path / ".env").exists()
    assert not (runtime / "staging").exists()
    assert not (runtime / "baseline-checks").exists()
    assert repository_fingerprint(repository) == before


def test_result_commit_uses_verified_candidate_not_model_index_and_preserves_source(repository):
    (repository / "src/app.py").write_text("local staged\n")
    git(repository, "add", "src/app.py")
    (repository / "src/app.py").write_text("local dirty\n")
    (repository / "untracked.txt").write_text("local untracked\n")
    before = repository_fingerprint(repository)
    policy = {"excluded_paths": ["private.txt"]}
    workspace = workspace_for(repository, policy=policy)
    (workspace.workspace / "src/app.py").write_text("answer = 2\n")
    (workspace.workspace / ".aiqa-output").mkdir()
    (workspace.workspace / ".aiqa-output/generated.txt").write_text("do not commit")
    git(workspace.workspace, "add", ".")
    frozen = freeze_candidate(workspace, policy)
    commit_started = int(time.time())
    result = commit_candidate(workspace, frozen, policy)
    assert git(workspace.workspace, "rev-parse", "HEAD").decode().strip() == result["commit"]
    assert git(workspace.workspace, "show", "HEAD:src/app.py") == b"answer = 2\n"
    assert git(workspace.workspace, "rev-parse", "HEAD^").decode().strip() == workspace.snapshot_base_commit_sha
    assert git(workspace.workspace, "log", "-1", "--format=%s").startswith(b"fix: apply verified AI QA result")
    assert int(git(workspace.workspace, "log", "-1", "--format=%ct")) >= commit_started
    assert b".aiqa-output" not in git(workspace.workspace, "ls-tree", "-r", "--name-only", "HEAD")
    assert git(workspace.workspace, "diff", "--cached").strip() == b""
    assert repository_fingerprint(repository) == before


def test_result_commit_rejects_changed_candidate(repository):
    workspace = workspace_for(repository)
    frozen = freeze_candidate(workspace, {})
    (frozen.path / "src/app.py").write_text("changed after verification")
    with pytest.raises(WorkspaceError, match="検証|검증"):
        commit_candidate(workspace, frozen, {})
    assert git(workspace.workspace, "rev-parse", "HEAD").decode().strip() == workspace.snapshot_base_commit_sha


def test_result_commit_does_not_follow_a_model_switched_branch(repository):
    workspace = workspace_for(repository)
    frozen = freeze_candidate(workspace, {"excluded_paths": ["private.txt"]})
    git(workspace.workspace, "switch", "-c", "temp/unexpected")
    with pytest.raises(WorkspaceError) as error:
        commit_candidate(workspace, frozen, {"excluded_paths": ["private.txt"]})
    assert error.value.code == "WORKSPACE_BRANCH_CHANGED"
    assert git(workspace.workspace, "rev-parse", "HEAD").decode().strip() == workspace.snapshot_base_commit_sha


def test_candidate_merges_hidden_baseline_and_preserves_binary_mode_and_deletions(repository):
    workspace = workspace_for(repository)
    (workspace.workspace / "src/app.py").write_bytes(b"answer = 42\n")
    (workspace.workspace / "readme.md").unlink()
    (workspace.workspace / "binary.dat").write_bytes(b"\0\xffbinary")
    (workspace.workspace / "script").write_bytes(b"#!/bin/sh\nexit 0\n")
    (workspace.workspace / "script").chmod(0o700)
    frozen = freeze_candidate(workspace, {"read_paths": ["."], "write_paths": ["."], "excluded_paths": ["private.txt"]})
    assert (frozen.path / "private.txt").read_bytes() == b"hidden source\n"
    assert (frozen.path / ".codex/config.toml").read_bytes() == b"malicious_source_config = true\n"
    assert (frozen.path / ".env").read_bytes() == b"SECRET=do-not-project\n"
    assert not (frozen.path / "readme.md").exists()
    assert (frozen.path / "binary.dat").read_bytes() == b"\0\xffbinary"
    assert (frozen.path / "script").stat().st_mode & stat.S_IXUSR
    assert tree_sha256(frozen.path) == frozen.tree_sha256 == tree_sha256(frozen.after_checks)
    assert frozen.changed_paths == ("binary.dat", "readme.md", "script", "src/app.py")
    (workspace.workspace / "src/app.py").write_bytes(b"post-freeze change\n")
    assert (frozen.path / "src/app.py").read_bytes() == b"answer = 42\n"


@pytest.mark.parametrize("path", ["readme.md", ".codex/config.toml", ".env", "private.txt"])
def test_unauthorized_modification_is_rejected(repository, path):
    policy = {"read_paths": ["."], "write_paths": ["src"], "excluded_paths": ["private.txt"]}
    workspace = workspace_for(repository, policy=policy)
    output = workspace.workspace / path
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"changed\n")
    with pytest.raises(WorkspaceError) as caught:
        freeze_candidate(workspace, policy)
    assert caught.value.code == "PATH_NOT_ALLOWED"
    assert not workspace.candidate.exists()


def test_committed_safe_symlink_is_preserved_and_hashed_by_target(repository):
    (repository / "alias.py").symlink_to("src/app.py")
    git(repository, "add", "alias.py")
    git(repository, "commit", "-qm", "Add safe symlink")
    workspace = workspace_for(repository)
    frozen = freeze_candidate(workspace, {"excluded_paths": ["private.txt"]})
    assert os.readlink(frozen.path / "alias.py") == "src/app.py"
    entry = next(item for item in tree_manifest(frozen.path) if item["path"] == "alias.py")
    assert entry == {"path": "alias.py", "kind": "symlink", "mode": 0o120000,
                     "sha256": hashlib.sha256(b"src/app.py").hexdigest()}


@pytest.mark.parametrize("target", ["/etc/passwd", "../outside", "missing", "link"])
def test_unsafe_committed_symlink_is_rejected_before_creating_runtime(repository, target):
    (repository / "link").symlink_to(target)
    git(repository, "add", "link")
    git(repository, "commit", "-qm", "Unsafe symlink fixture")
    with pytest.raises(WorkspaceError):
        workspace_for(repository)
    assert not (repository.parent / "runtime").exists()


def test_new_symlink_and_target_change_are_rejected(repository):
    (repository / "alias").symlink_to("src/app.py")
    git(repository, "add", "alias")
    git(repository, "commit", "-qm", "Existing link")
    first = workspace_for(repository)
    (first.workspace / "newlink").symlink_to("src/app.py")
    with pytest.raises(WorkspaceError):
        freeze_candidate(first, {"excluded_paths": ["private.txt"]})
    second = workspace_for(repository)
    (second.workspace / "alias").unlink()
    (second.workspace / "alias").symlink_to("readme.md")
    with pytest.raises(WorkspaceError):
        freeze_candidate(second, {"excluded_paths": ["private.txt"]})


def test_hidden_symlink_target_is_not_exposed(repository):
    (repository / "alias").symlink_to("private.txt")
    git(repository, "add", "alias")
    git(repository, "commit", "-qm", "Excluded link target")
    with pytest.raises(WorkspaceError):
        workspace_for(repository)


def test_committed_directory_symlink_is_preserved(repository):
    (repository / "source-alias").symlink_to("src", target_is_directory=True)
    git(repository, "add", "source-alias")
    git(repository, "commit", "-qm", "Directory link")
    workspace = workspace_for(repository)
    assert (workspace.workspace / "source-alias/app.py").read_text() == "answer = 1\n"
    frozen = freeze_candidate(workspace, {"excluded_paths": ["private.txt"]})
    assert os.readlink(frozen.path / "source-alias") == "src"


def test_directory_symlink_cycle_is_rejected(repository):
    (repository / "src/loop").symlink_to("../src", target_is_directory=True)
    git(repository, "add", "src/loop")
    git(repository, "commit", "-qm", "Directory cycle")
    with pytest.raises(WorkspaceError):
        workspace_for(repository)


def test_excessive_symlink_target_is_rejected_before_materialization(repository):
    target = b"./" * 2100 + b"src/app.py"
    object_id = git(repository, "hash-object", "-w", "--stdin", input_data=target).decode().strip()
    git(repository, "update-index", "--add", "--cacheinfo", f"120000,{object_id},long-link")
    git(repository, "commit", "-qm", "Long symlink target fixture")
    with pytest.raises(WorkspaceError):
        workspace_for(repository)
    assert not (repository.parent / "runtime").exists()


@pytest.mark.parametrize("kind", ["lfs", "gitlink", "reserved"])
def test_unsupported_snapshot_cannot_silently_omit_files(repository, kind):
    if kind == "lfs":
        (repository / "large.dat").write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 100\n")
        git(repository, "add", "large.dat")
    elif kind == "reserved":
        (repository / ".aiqa-output").mkdir()
        (repository / ".aiqa-output/file").write_text("source")
        git(repository, "add", ".aiqa-output/file")
    else:
        sha = git(repository, "rev-parse", "HEAD").decode().strip()
        git(repository, "update-index", "--add", "--cacheinfo", f"160000,{sha},submodule")
    git(repository, "commit", "-qm", "Unsupported fixture")
    with pytest.raises(WorkspaceError) as caught:
        workspace_for(repository)
    assert caught.value.code == "UNSUPPORTED_SNAPSHOT"


def test_git_attributes_filters_and_hooks_are_never_executed(repository):
    sentinel = repository.parent / "executed"
    (repository / ".gitattributes").write_text("*.py filter=danger\n")
    git(repository, "add", ".gitattributes")
    git(repository, "commit", "-qm", "Attribute fixture")
    git(repository, "config", "filter.danger.clean", f"touch {sentinel}")
    git(repository, "config", "filter.danger.smudge", f"touch {sentinel}")
    git(repository, "config", "core.fsmonitor", f"touch {sentinel}")
    hook = repository / ".git/hooks/post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    hook.chmod(0o700)
    workspace = workspace_for(repository)
    assert (workspace.workspace / "src/app.py").read_bytes() == b"answer = 1\n"
    assert not sentinel.exists()


def test_no_overwrite_and_runtime_symlink_are_rejected(repository):
    run_id = uuid4()
    sha = git(repository, "rev-parse", "HEAD").decode().strip()
    data = repository.parent / "runtime"
    first = create_workspace(repository, sha, run_id, {}, data)
    before = tree_sha256(first.workspace)
    with pytest.raises(WorkspaceError) as caught:
        create_workspace(repository, sha, run_id, {}, data)
    assert caught.value.code == "WORKSPACE_EXISTS"
    assert tree_sha256(first.workspace) == before
    alias = repository.parent / "runtime-alias"
    alias.symlink_to(data, target_is_directory=True)
    with pytest.raises(WorkspaceError) as caught:
        create_workspace(repository, sha, uuid4(), {}, alias)
    assert caught.value.code == "PATH_NOT_ALLOWED"


def test_baseline_tampering_is_rejected(repository):
    workspace = workspace_for(repository)
    (workspace.full_baseline / "private.txt").write_bytes(b"tampered baseline")
    with pytest.raises(WorkspaceError) as caught:
        freeze_candidate(workspace, {})
    assert caught.value.code == "RESULT_INCOMPLETE"


def test_manifest_digest_matches_artifact_contract(repository):
    workspace = workspace_for(repository)
    manifest = tree_manifest(workspace.full_baseline)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert manifest == sorted(manifest, key=lambda entry: entry["path"])
    assert hashlib.sha256(encoded).hexdigest() == workspace.baseline_tree_sha256
    assert tree_sha256(workspace.full_baseline) == workspace.baseline_tree_sha256


def test_collector_rejects_fifo_without_opening_it(repository):
    workspace = workspace_for(repository)
    os.mkfifo(workspace.workspace / "pipe")
    with pytest.raises(WorkspaceError):
        freeze_candidate(workspace, {})


def test_generated_environments_are_separate_from_candidate_and_source_hash(repository):
    policy = {"excluded_paths": ["private.txt"], "verification_commands": [{"generated_paths": [".aiqa-output", "cache"]}]}
    workspace = workspace_for(repository, policy=policy)
    (workspace.workspace / ".venv/bin").mkdir(parents=True)
    (workspace.workspace / ".venv/bin/python").symlink_to("/usr/bin/python3")
    (workspace.workspace / "cache").mkdir()
    (workspace.workspace / "cache/output").write_bytes(b"not source")
    (workspace.workspace / "src/app.py").write_bytes(b"fixed = True\n")
    frozen = freeze_candidate(workspace, policy)
    assert not (frozen.path / ".venv").exists()
    assert not (frozen.after_checks / "cache").exists()
    assert frozen.changed_paths == ("src/app.py",)
    (frozen.after_checks / ".aiqa-output").mkdir()
    (frozen.after_checks / ".aiqa-output/report").write_bytes(b"check evidence")
    assert source_tree_sha256(frozen.after_checks, policy) == frozen.tree_sha256


def test_generated_paths_cannot_hide_committed_source(repository):
    with pytest.raises(WorkspaceError) as caught:
        workspace_for(repository, policy={"verification_commands": [{"generated_paths": ["src"]}]})
    assert caught.value.code == "INVALID_POLICY"


def test_python_verification_bytecode_is_not_a_source_change(repository):
    policy = {"excluded_paths": ["private.txt"]}
    workspace = workspace_for(repository, policy=policy)
    for path in ("__pycache__", "src/__pycache__"):
        directory = workspace.workspace / path
        directory.mkdir()
        (directory / "app.cpython-312.pyc").write_bytes(b"generated bytecode")
    (workspace.workspace / "src/app.py").write_bytes(b"answer = 2\n")
    frozen = freeze_candidate(workspace, policy)
    assert frozen.changed_paths == ("src/app.py",)
    result = commit_candidate(workspace, frozen, policy)
    assert result["commit"]
    assert b"__pycache__" not in git(workspace.workspace, "ls-tree", "-r", "--name-only", "HEAD")


def test_hardlink_cannot_import_original_file_into_candidate(repository):
    workspace = workspace_for(repository)
    os.link(repository / "private.txt", workspace.workspace / "stolen.txt")
    with pytest.raises(WorkspaceError):
        freeze_candidate(workspace, {})


def test_application_hooks_directory_is_editable_but_execution_hooks_are_hidden(repository):
    (repository / "src/hooks").mkdir()
    (repository / "src/hooks/use.py").write_bytes(b"application hook")
    (repository / ".husky").mkdir()
    (repository / ".husky/pre-commit").write_bytes(b"execution hook")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Hook scopes")
    workspace = workspace_for(repository)
    assert (workspace.workspace / "src/hooks/use.py").exists()
    assert not (workspace.workspace / ".husky").exists()
