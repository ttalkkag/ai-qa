import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from apps.errors import DomainError
from .storage import canonical_json, data_root, digest, relative_key


def collect_tree(directory):
    from apps.runs.workspace import collect_tree as collect_source_tree
    source = collect_source_tree(directory)
    entries = [{"path": path, "kind": entry.kind, "mode": entry.mode, "sha256": digest(entry.content)} for path, entry in sorted(source.items())]
    return entries, {path: entry.content for path, entry in source.items()}


def tree_manifest(directory):
    return collect_tree(directory)[0]


def tree_digest(directory):
    return digest(canonical_json(tree_manifest(directory)))


def materialize_tree(entries, contents, destination):
    destination = Path(destination)
    if destination.exists() and (destination.is_symlink() or any(destination.iterdir())):
        raise DomainError("DESTINATION_NOT_EMPTY")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for row in entries:
        relative_key(row["path"])
        target = destination / row["path"]
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = contents[row["path"]]
        if digest(content) != row["sha256"]:
            raise DomainError("ARTIFACT_CORRUPTED")
        if row["kind"] == "symlink":
            link = content.decode("utf-8")
            if Path(link).is_absolute():
                raise DomainError("UNSAFE_SOURCE_SYMLINK")
            try:
                (target.parent / link).resolve().relative_to(destination.resolve())
            except (ValueError, RuntimeError) as exc:
                raise DomainError("UNSAFE_SOURCE_SYMLINK") from exc
            target.symlink_to(link)
        else:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, row["mode"] & 0o777)
            with os.fdopen(fd, "wb") as out:
                out.write(content)
            target.chmod(row["mode"] & 0o777)
    return destination


def _git(directory, *args, input=None):
    executable = shutil.which("git")
    if not executable:
        raise DomainError("GIT_NOT_FOUND")
    env = {"PATH": os.defpath, "HOME": str(directory), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    result = subprocess.run([executable, "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "core.filemode=true", *args], cwd=directory, env=env, input=input, capture_output=True, timeout=60)
    if result.returncode:
        raise DomainError("CANONICAL_PATCH_FAILED")
    return result.stdout


def make_patch(baseline, baseline_content, candidate, candidate_content):
    baseline_map = {row["path"]: row for row in baseline}
    candidate_map = {row["path"]: row for row in candidate}
    for path in set(baseline_map) | set(candidate_map):
        before, after = baseline_map.get(path), candidate_map.get(path)
        if (before and before["kind"] == "symlink") or (after and after["kind"] == "symlink"):
            if before != after:
                raise DomainError("SOURCE_SYMLINK_CHANGED")
    staging = data_root() / "staging"
    staging.mkdir(mode=0o700, exist_ok=True)
    if staging.is_symlink():
        raise DomainError("UNSAFE_ARTIFACT_PATH")
    with tempfile.TemporaryDirectory(prefix="patch-", dir=staging) as temporary:
        root = Path(temporary)
        repository = root / "repository"
        materialize_tree(baseline, baseline_content, repository)
        _git(repository, "init", "--quiet", "--template=")
        _git(repository, "add", "--all", "--force", "--", ".")
        baseline_tree = _git(repository, "write-tree").decode().strip()
        for child in repository.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        replacement = root / "replacement"
        materialize_tree(candidate, candidate_content, replacement)
        for child in replacement.iterdir():
            child.rename(repository / child.name)
        _git(repository, "add", "--all", "--force", "--", ".")
        patch = _git(repository, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames", baseline_tree, "--")
        replay = root / "replay"
        materialize_tree(baseline, baseline_content, replay)
        if patch:
            _git(replay, "apply", "--binary", "-", input=patch)
        if tree_digest(replay) != digest(canonical_json(candidate)):
            raise DomainError("CANONICAL_PATCH_MISMATCH")
        return patch
