import hashlib
import json
import os
import stat
from pathlib import Path

from apps.errors import DomainError
from .workspace import _git


def _file_hash(path, root):
    current = path
    while current != root:
        if current.parent == current:
            raise DomainError("PATH_NOT_ALLOWED")
        if current != path and current.is_symlink():
            return {"kind": "indirect", "target": os.readlink(current)}
        current = current.parent
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    if stat.S_ISLNK(metadata.st_mode):
        return {"kind": "symlink", "target": os.readlink(path)}
    if not stat.S_ISREG(metadata.st_mode):
        return {"kind": "other", "mode": metadata.st_mode}
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise DomainError("ORIGINAL_CHANGED")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise DomainError("ORIGINAL_CHANGED")
    return {"kind": "file", "mode": metadata.st_mode, "sha256": digest.hexdigest()}


def repository_fingerprint(repo):
    repo = Path(repo)
    paths = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
    manifest = []
    for raw in sorted(set(paths) - {b""}):
        relative = os.fsdecode(raw)
        item = Path(relative)
        if item.is_absolute() or ".." in item.parts:
            raise DomainError("PATH_NOT_ALLOWED")
        manifest.append({"path": relative, **_file_hash(repo / item, repo)})
    index_path = Path(os.fsdecode(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()))
    index = _file_hash(index_path, Path(index_path.anchor))
    refs = hashlib.sha256(_git(repo, "show-ref", "--head")).hexdigest()
    return {
        "tracked_and_untracked_sha256": hashlib.sha256(json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "index": index, "refs_sha256": refs, "scope": "tracked and nonignored untracked files; ignored files are excluded",
    }
