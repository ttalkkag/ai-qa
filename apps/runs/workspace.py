from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

from apps.errors import DomainError


class WorkspaceError(DomainError):
    pass


@dataclass(frozen=True)
class TreeEntry:
    mode: int
    content: bytes

    @property
    def kind(self):
        return "symlink" if self.mode == 0o120000 else "file"


@dataclass(frozen=True)
class Workspace:
    full_baseline: Path
    workspace: Path
    baseline_checks: Path
    candidate: Path
    after_checks: Path
    snapshot_base_commit_sha: str
    baseline_tree_sha256: str


@dataclass(frozen=True)
class FrozenCandidate:
    path: Path
    after_checks: Path
    tree_sha256: str
    changed_paths: tuple[str, ...]


_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_TREE_BYTES = 256 * 1024 * 1024
_MAX_FILES = 100_000
_PROTECTED = {".codex", ".claude", ".agents", ".skills", ".git", ".githooks", ".husky"}
_PROTECTED_FILES = {"agents.md", "claude.md"}
_DEFAULT_EXCLUDES = (".env", ".env.*", "*.pem", "*.key", ".venv", "venv", "node_modules", "__pycache__")
_RESERVED = {".aiqa-inputs", ".aiqa-output"}


def _fail(message, code="UNSUPPORTED_SNAPSHOT"):
    raise WorkspaceError(code, message)


def _validate_path(value, *, allow_metadata=False):
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("지원하지 않는 파일 경로입니다.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail("파일 경로에 제어 문자가 있습니다.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or (not allow_metadata and any(part.casefold() == ".git" for part in parts)):
        _fail("파일 경로가 허용 범위를 벗어납니다.")
    try:
        if len(value.encode("utf-8")) > 4096 or any(len(part.encode("utf-8")) > 255 for part in parts):
            _fail("파일 경로 길이를 지원하지 않습니다.")
    except UnicodeError as exc:
        raise WorkspaceError("UNSUPPORTED_SNAPSHOT", "파일 경로는 UTF-8이어야 합니다.") from exc
    return value


def _ensure_no_collisions(entries):
    seen = {}
    for path in entries:
        for count in range(1, len(path.split("/")) + 1):
            part = "/".join(path.split("/")[:count])
            key = unicodedata.normalize("NFC", part).casefold()
            if key in seen and seen[key] != part:
                _fail("파일시스템에서 충돌하는 경로가 있습니다.")
            seen[key] = part
        if any(parent.as_posix() in entries for parent in PurePosixPath(path).parents if parent.as_posix() != "."):
            _fail("파일과 디렉터리 경로가 충돌합니다.")


def _git(root, *arguments, input_data=None, index_file=None, commit_timestamp=None):
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "", "GIT_AUTHOR_NAME": "AI QA",
        "GIT_AUTHOR_EMAIL": "snapshot@localhost", "GIT_COMMITTER_NAME": "AI QA",
        "GIT_COMMITTER_EMAIL": "snapshot@localhost",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    if commit_timestamp is not None:
        environment["GIT_AUTHOR_DATE"] = environment["GIT_COMMITTER_DATE"] = commit_timestamp
    command = [
        "/usr/bin/git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
        "-c", "protocol.allow=never", "-c", "commit.gpgSign=false", "-C", str(root), *arguments,
    ]
    try:
        completed = subprocess.run(command, input=input_data, capture_output=True, env=environment, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError("SNAPSHOT_FAILED", "Git 기준을 안전하게 읽지 못했습니다.") from exc
    if completed.returncode:
        _fail("Git 기준을 안전하게 읽지 못했습니다.", "SNAPSHOT_FAILED")
    return completed.stdout


def _source_entries(repo_path, sha):
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        _fail("전체 commit SHA가 필요합니다.")
    if _git(repo_path, "cat-file", "-t", sha).strip() != b"commit":
        _fail("실행 기준이 commit이 아닙니다.")
    entries = {}
    total = 0
    records = _git(repo_path, "ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0")
    if len(records) > _MAX_FILES + 1:
        _fail("기준 파일 수가 안전 수집 한도를 초과했습니다.")
    for record in records:
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_bytes, kind, object_id = metadata.split(b" ")
            path = _validate_path(path_bytes.decode("utf-8"))
            mode = int(mode_bytes, 8)
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceError("UNSUPPORTED_SNAPSHOT", "Git tree 형식을 지원하지 않습니다.") from exc
        if path.split("/")[0].casefold() in _RESERVED:
            _fail("기준 tree가 앱 예약 경로를 사용합니다.")
        if kind != b"blob" or mode not in {0o100644, 0o100755, 0o120000}:
            _fail("submodule 또는 지원하지 않는 파일 종류가 있습니다.")
        size = int(_git(repo_path, "cat-file", "-s", object_id.decode("ascii")).strip())
        total += size
        if size > _MAX_FILE_BYTES or total > _MAX_TREE_BYTES:
            _fail("기준 파일 크기가 안전 수집 한도를 초과했습니다.")
        content = _git(repo_path, "cat-file", "blob", object_id.decode("ascii"))
        if len(content) != size:
            _fail("Git blob을 완전히 읽지 못했습니다.")
        if content.startswith(b"version https://git-lfs.github.com/spec/v1"):
            _fail("Git LFS pointer는 로컬 snapshot에서 지원하지 않습니다.")
        entries[path] = TreeEntry(mode, content)
    _ensure_no_collisions(entries)
    _validate_links(entries)
    return entries


def _validate_links(entries):
    directories = {parent.as_posix() for path in entries for parent in PurePosixPath(path).parents if parent.as_posix() != "."}
    def follow(path, visited):
        if path in visited:
            _fail("symlink 대상이 없거나 순환합니다.")
        if path in directories:
            for child, entry in entries.items():
                if child.startswith(path + "/") and entry.kind == "symlink":
                    follow(child, visited | {path})
            return path
        if path not in entries:
            _fail("symlink 대상이 없거나 순환합니다.")
        entry = entries[path]
        if entry.kind != "symlink":
            return path
        try:
            target = entry.content.decode("utf-8")
        except UnicodeError as exc:
            raise WorkspaceError("UNSUPPORTED_SNAPSHOT", "symlink 대상 형식을 지원하지 않습니다.") from exc
        if not target or len(entry.content) > 4096 or target.startswith("/") or "\\" in target or any(ord(char) < 32 or ord(char) == 127 for char in target):
            _fail("symlink 대상이 허용 범위를 벗어납니다.")
        parts = list(PurePosixPath(path).parent.parts)
        for part in target.split("/"):
            if part == "..":
                if not parts:
                    _fail("symlink 대상이 사본 밖으로 나갑니다.")
                parts.pop()
            elif part not in {"", "."}:
                parts.append(part)
        resolved = "/".join(parts)
        return follow(resolved, visited | {path})

    for path, entry in entries.items():
        if entry.kind == "symlink":
            follow(path, set())


def _policy_list(policy, key, default):
    value = policy.get(key, default) if isinstance(policy, dict) else getattr(policy, key, default)
    if not isinstance(value, (list, tuple)) or not all(isinstance(rule, str) for rule in value):
        _fail("경로 정책 형식이 올바르지 않습니다.", "INVALID_POLICY")
    for rule in value:
        if rule != ".":
            _validate_path(rule.rstrip("/"), allow_metadata=True)
    return value


def _matches(path, rule, any_component=False):
    rule = rule.rstrip("/")
    return rule == "." or path == rule or path.startswith(rule + "/") or fnmatch.fnmatchcase(path, rule) or (
        any_component and "/" not in rule and any(fnmatch.fnmatchcase(part, rule) for part in path.split("/"))
    )


def _allowed(path, policy, operation):
    parts = path.split("/")
    if any(part.casefold() in _PROTECTED or part.casefold() in _PROTECTED_FILES for part in parts):
        return False
    if parts[0].casefold() in {"hooks", "skills"}:
        return False
    excludes = (*_DEFAULT_EXCLUDES, *_policy_list(policy, "excluded_paths", []))
    if any(_matches(path, rule, True) for rule in excludes):
        return False
    reads = _policy_list(policy, "read_paths", ["."])
    writes = _policy_list(policy, "write_paths", ["."])
    return any(_matches(path, rule) for rule in reads) and (
        operation == "read" or any(_matches(path, rule) for rule in writes)
    )


def _safe_directory(path, *, create=False):
    path = Path(path).absolute()
    descriptor = _open_directory(path, create=create, allow_missing=not create)
    if descriptor is not None:
        os.close(descriptor)
    return path


def _open_directory(path, *, create=False, allow_missing=False):
    path = Path(path).absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                _fail("저장 경로가 정규 절대 경로가 아닙니다.", "PATH_NOT_ALLOWED")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if allow_missing:
                    os.close(descriptor)
                    return None
                raise
            except OSError as exc:
                raise WorkspaceError("PATH_NOT_ALLOWED", "저장 경로에 symlink 또는 비디렉터리가 있습니다.") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _materialize(entries, destination):
    _ensure_no_collisions(entries)
    _validate_links(entries)
    parent = _open_directory(destination.parent, create=True)
    try:
        os.mkdir(destination.name, mode=0o700, dir_fd=parent)
        root = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        for path, entry in sorted(entries.items()):
            directory = os.dup(root)
            try:
                parts = path.split("/")
                for part in parts[:-1]:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                if entry.kind == "symlink":
                    os.symlink(entry.content.decode("utf-8"), parts[-1], dir_fd=directory)
                else:
                    descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o700 if entry.mode == 0o100755 else 0o600, dir_fd=directory)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(entry.content)
            finally:
                os.close(directory)
    finally:
        os.close(root)


def _initialize_git(destination, entries):
    _git(destination, "init", "--quiet", "--template=", "--initial-branch=aiqa-snapshot")
    index = []
    for path, entry in sorted(entries.items()):
        object_id = _git(destination, "hash-object", "-w", "--stdin", input_data=entry.content).strip()
        index.append(f"{entry.mode:o} ".encode() + object_id + b"\t" + path.encode("utf-8") + b"\0")
    _git(destination, "update-index", "-z", "--index-info", input_data=b"".join(index))
    tree_id = _git(destination, "write-tree").strip().decode("ascii")
    commit_id = _git(destination, "commit-tree", tree_id, input_data=b"AI QA independent snapshot\n").strip().decode("ascii")
    _git(destination, "update-ref", "--no-deref", "HEAD", commit_id)
    return commit_id


def collect_tree(root, *, excluded_paths=()):
    root = _safe_directory(root)
    entries = {}
    total = 0

    def visit(directory_fd, prefix):
        nonlocal total
        directory_before = os.fstat(directory_fd)
        for name in sorted(os.listdir(directory_fd)):
            path = f"{prefix}/{name}" if prefix else name
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not prefix and name == ".git":
                if stat.S_ISREG(before.st_mode):
                    expected = root.parent.parent / "staging" / root.name / "worktree-repo" / ".git" / "worktrees" / root.name
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        marker = os.read(descriptor, 4097)
                    finally:
                        os.close(descriptor)
                    if marker != f"gitdir: {expected}\n".encode():
                        _fail("작업 사본 Git metadata가 변조되었습니다.")
                elif not stat.S_ISDIR(before.st_mode):
                    _fail("작업 사본 Git metadata가 변조되었습니다.")
                continue
            _validate_path(path)
            if any(_matches(path, rule) for rule in excluded_paths):
                continue
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        _fail("수집 중 디렉터리가 교체되었습니다.")
                    visit(child, path)
                finally:
                    os.close(child)
                continue
            if stat.S_ISLNK(before.st_mode):
                content = os.readlink(name, dir_fd=directory_fd).encode("utf-8")
                mode = 0o120000
            elif stat.S_ISREG(before.st_mode) and not before.st_mode & 0o7000 and before.st_nlink == 1:
                if before.st_size > _MAX_FILE_BYTES:
                    _fail("파일 크기가 안전 수집 한도를 초과했습니다.")
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        _fail("수집 중 파일이 교체되었습니다.")
                    content = stream.read(_MAX_FILE_BYTES + 1)
                    after_read = os.fstat(stream.fileno())
                    if _identity(opened) != _identity(after_read) or len(content) != before.st_size:
                        _fail("수집 중 파일이 변경되었습니다.")
                mode = 0o100755 if before.st_mode & 0o111 else 0o100644
            else:
                _fail("특수 파일 또는 지원하지 않는 mode입니다.")
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(before) != _identity(after):
                _fail("수집 중 파일이 변경되었습니다.")
            total += len(content)
            if total > _MAX_TREE_BYTES or len(entries) >= _MAX_FILES:
                _fail("파일 수집 한도를 초과했습니다.")
            entries[path] = TreeEntry(mode, content)
        if _identity(directory_before) != _identity(os.fstat(directory_fd)):
            _fail("수집 중 디렉터리가 변경되었습니다.")

    descriptor = _open_directory(root)
    try:
        visit(descriptor, "")
    finally:
        os.close(descriptor)
    _ensure_no_collisions(entries)
    _validate_links(entries)
    return entries


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _generated_paths(policy):
    paths = {".venv", ".aiqa-inputs", ".aiqa-output", "__pycache__", "**/__pycache__", "**/__pycache__/**"}
    commands = policy.get("verification_commands", []) if isinstance(policy, dict) else getattr(policy, "verification_commands", [])
    for command in commands:
        paths.update(_policy_list(command, "generated_paths", [".aiqa-output"]))
    return tuple(sorted(paths))


def _manifest(entries):
    return [{"path": path, "kind": entry.kind, "mode": entry.mode, "sha256": hashlib.sha256(entry.content).hexdigest()}
            for path, entry in sorted(entries.items())]


def _digest(entries):
    return hashlib.sha256(json.dumps(_manifest(entries), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def tree_manifest(root, *, excluded_paths=()):
    return _manifest(collect_tree(root, excluded_paths=excluded_paths))


def tree_sha256(root, *, excluded_paths=()):
    return _digest(collect_tree(root, excluded_paths=excluded_paths))


def source_tree_sha256(root, policy):
    return tree_sha256(root, excluded_paths=_generated_paths(policy))


def create_workspace(repo_path, sha, run_id, policy, data_dir):
    repo = _safe_directory(repo_path)
    data = _safe_directory(data_dir)
    try:
        run_name = str(UUID(str(run_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkspaceError("PATH_NOT_ALLOWED", "실행 식별자가 올바르지 않습니다.") from exc
    if data == repo or data in repo.parents or repo in data.parents:
        _fail("원본과 실행 데이터 경로가 중첩됩니다.", "PATH_NOT_ALLOWED")
    top = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").strip())).resolve()
    if top != repo.resolve():
        _fail("원본은 Git 저장소 root여야 합니다.", "PATH_NOT_ALLOWED")
    baseline = _source_entries(repo, sha)
    generated = _generated_paths(policy)
    if any(_matches(path, rule) for path in baseline for rule in generated):
        _fail("생성 경로에 committed source가 있습니다.", "INVALID_POLICY")
    projection = {path: entry for path, entry in baseline.items() if _allowed(path, policy, "read")}
    _validate_links(projection)
    full_path = data / "staging" / run_name / "baseline"
    edit_path = data / "workspaces" / run_name
    before_path = data / "baseline-checks" / run_name
    candidate_path = data / "staging" / run_name / "candidate"
    after_path = data / "after-checks" / run_name
    for path in (full_path, edit_path, before_path, candidate_path, after_path):
        _safe_directory(path.parent)
        if path.exists() or path.is_symlink():
            _fail("기존 실행 사본은 덮어쓰지 않습니다.", "WORKSPACE_EXISTS")
    _materialize(baseline, full_path)
    worktree_repo = data / "staging" / run_name / "worktree-repo"
    _materialize(projection, worktree_repo)
    snapshot_sha = _initialize_git(worktree_repo, projection)
    _safe_directory(edit_path.parent, create=True)
    _git(worktree_repo, "worktree", "add", "-b", f"temp/aiqa-{run_name}", str(edit_path), snapshot_sha)
    _materialize(baseline, before_path)
    _initialize_git(before_path, baseline)
    return Workspace(full_path, edit_path, before_path, candidate_path, after_path, snapshot_sha, _digest(baseline))


def create_readonly_workspace(repo_path, sha, invocation_id, policy, data_dir):
    repo = _safe_directory(repo_path)
    data = _safe_directory(data_dir)
    try:
        name = str(UUID(str(invocation_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkspaceError("PATH_NOT_ALLOWED", "호출 식별자가 올바르지 않습니다.") from exc
    if data == repo or data in repo.parents or repo in data.parents:
        _fail("원본과 대화 사본 경로가 중첩됩니다.", "PATH_NOT_ALLOWED")
    baseline = _source_entries(repo, sha)
    if any(_matches(path, rule) for path in baseline for rule in _generated_paths(policy)):
        _fail("생성 경로에 committed source가 있습니다.", "INVALID_POLICY")
    projection = {path: entry for path, entry in baseline.items() if _allowed(path, policy, "read")}
    _validate_links(projection)
    destination = data / "conversations-tmp" / name
    _safe_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        _fail("기존 대화 사본은 덮어쓰지 않습니다.", "WORKSPACE_EXISTS")
    _materialize(projection, destination)
    _initialize_git(destination, projection)
    return destination


def freeze_candidate(workspace, policy):
    baseline = collect_tree(workspace.full_baseline)
    if _digest(baseline) != workspace.baseline_tree_sha256:
        _fail("보존된 기준 tree가 변경되었습니다.", "RESULT_INCOMPLETE")
    generated = _generated_paths(policy)
    if any(_matches(path, rule) for path in baseline for rule in generated):
        _fail("생성 경로에 committed source가 있습니다.", "INVALID_POLICY")
    current = collect_tree(workspace.workspace, excluded_paths=generated)
    projected = {path: entry for path, entry in baseline.items() if _allowed(path, policy, "read")}
    candidate = dict(baseline)
    changed = []
    for path in sorted(projected.keys() | current.keys()):
        before, after = projected.get(path), current.get(path)
        if before == after:
            continue
        if not _allowed(path, policy, "write") or path.split("/")[0].casefold() in _RESERVED:
            _fail("허용 범위 밖의 파일 변경이 있습니다.", "PATH_NOT_ALLOWED")
        if (before and before.kind == "symlink") or (after and after.kind == "symlink"):
            _fail("symlink 추가·삭제·변경은 지원하지 않습니다.")
        changed.append(path)
        if after is None:
            candidate.pop(path, None)
        else:
            candidate[path] = after
    _ensure_no_collisions(candidate)
    _validate_links(candidate)
    _materialize(candidate, workspace.candidate)
    _materialize(candidate, workspace.after_checks)
    _initialize_git(workspace.after_checks, candidate)
    return FrozenCandidate(workspace.candidate, workspace.after_checks, _digest(candidate), tuple(changed))


def commit_candidate(workspace, frozen, policy):
    entries = collect_tree(frozen.path)
    if _digest(entries) != frozen.tree_sha256:
        _fail("검증한 결과 사본이 변경되었습니다.", "SOURCE_CHANGED_DURING_CHECK")
    projection = {path: entry for path, entry in entries.items() if _allowed(path, policy, "read")}
    repo = workspace.full_baseline.parent / "worktree-repo"
    branch = f"temp/aiqa-{workspace.workspace.name}"
    if _git(workspace.workspace, "rev-parse", "--abbrev-ref", "HEAD").decode().strip() != branch:
        _fail("작업 브랜치가 변경되어 결과 커밋을 중단했습니다.", "WORKSPACE_BRANCH_CHANGED")
    # Build from the verified candidate, never the model's index or generated files.
    with tempfile.TemporaryDirectory(prefix="aiqa-index-") as directory:
        index_file = Path(directory) / "index"
        _git(repo, "read-tree", "--empty", index_file=index_file)
        records = []
        for path, entry in sorted(projection.items()):
            oid = _git(repo, "hash-object", "-w", "--stdin", input_data=entry.content).strip()
            records.append(f"{entry.mode:o} ".encode() + oid + b"\t" + path.encode() + b"\0")
        _git(repo, "update-index", "-z", "--index-info", input_data=b"".join(records), index_file=index_file)
        tree = _git(repo, "write-tree", index_file=index_file).decode().strip()
    message = f"fix: apply verified AI QA result {workspace.workspace.name}\n"
    commit = _git(repo, "commit-tree", tree, "-p", workspace.snapshot_base_commit_sha,
                  input_data=message.encode(), commit_timestamp=f"{int(time.time())} +0000").decode().strip()
    _git(repo, "update-ref", f"refs/heads/{branch}", commit, workspace.snapshot_base_commit_sha)
    _git(workspace.workspace, "read-tree", commit)
    return {"branch": branch, "commit": commit}
