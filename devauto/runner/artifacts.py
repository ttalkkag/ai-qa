from __future__ import annotations

import re
from pathlib import Path

from devauto.core.db import Database
from devauto.core.models import Artifact
from devauto.core.policy import SECRET_KEY_NAME


SECRET_PATTERNS = [
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/?#\s@]+)(@)"),
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)([^\r\n]+)"),
    re.compile(r"(?i)\b((?:set-cookie|cookie)\s*[:=]\s*)([^\r\n]+)"),
    re.compile(rf"(?i)(?<![A-Za-z0-9_-])(--?{SECRET_KEY_NAME}(?:\s+|=))([^\s,;}}]+)"),
    re.compile(
        rf"(?i)(?<![A-Za-z0-9])({SECRET_KEY_NAME})"
        r"(\s*[:=]\s*)"
        r"([\"']?)"
        r"([^\"'\s,;}]+)"
        r"([\"']?)"
    ),
    re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]+)"),
]


def validate_artifact_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("artifact name이 필요합니다")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("artifact name은 일반 파일명이어야 합니다")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("artifact name에 제어 문자가 포함되어 있습니다")
    return value


def safe_artifact_path(data_root: Path, run_id: str, path: Path) -> Path:
    root = data_root.resolve()
    base = safe_artifacts_dir(data_root, run_id)
    if path.is_symlink():
        raise ValueError("artifact path는 symlink일 수 없습니다")
    resolved = path.resolve()
    try:
        base.relative_to(root)
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("artifact path가 run artifact directory 밖으로 벗어났습니다") from exc
    return resolved


def safe_artifacts_dir(data_root: Path, run_id: str, create: bool = False) -> Path:
    root = data_root.resolve()
    runs_dir = root / "runs"
    if runs_dir.is_symlink():
        raise ValueError("artifact runs directory는 symlink일 수 없습니다")
    if create:
        runs_dir.mkdir(parents=True, exist_ok=True)

    run_dir = runs_dir / run_id
    if run_dir.is_symlink():
        raise ValueError("artifact run directory는 symlink일 수 없습니다")
    if create:
        run_dir.mkdir(parents=True, exist_ok=True)
    run_dir_resolved = run_dir.resolve()
    try:
        run_dir_resolved.relative_to(runs_dir.resolve())
    except ValueError as exc:
        raise ValueError("artifact run directory가 data root 밖으로 벗어났습니다") from exc

    artifacts_dir = run_dir / "artifacts"
    if artifacts_dir.is_symlink():
        raise ValueError("artifact 디렉터리는 symlink일 수 없습니다")
    if create:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir_resolved = artifacts_dir.resolve()
    try:
        artifacts_dir_resolved.relative_to(run_dir_resolved)
    except ValueError as exc:
        raise ValueError("artifact directory가 run directory 밖으로 벗어났습니다") from exc
    return artifacts_dir_resolved


def read_artifact_text(data_root: Path, artifact: Artifact) -> str:
    validate_artifact_name(artifact.name)
    path = safe_artifact_path(data_root, artifact.run_id, artifact.path)
    return path.read_text(encoding="utf-8")


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 5:
            redacted = pattern.sub(r"\1\2\3[REDACTED]\5", redacted)
        elif pattern.groups == 3:
            redacted = pattern.sub(r"\1[REDACTED]\3", redacted)
        else:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


class ArtifactStore:
    def __init__(self, data_root: Path, db: Database) -> None:
        self.data_root = data_root
        self.db = db

    def run_dir(self, run_id: str) -> Path:
        return self.data_root / "runs" / run_id

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def write_text(self, run_id: str, kind: str, name: str, content: str) -> Path:
        artifact_name = validate_artifact_name(name)
        artifact_dir = safe_artifacts_dir(self.data_root, run_id, create=True)
        path = artifact_dir / artifact_name
        path = safe_artifact_path(self.data_root, run_id, path)
        path.write_text(redact(content), encoding="utf-8")
        self.db.add_artifact(run_id, kind, artifact_name, path)
        return path

    def read_text(self, run_id: str, name: str) -> str:
        artifact = self.db.get_artifact(run_id, name)
        return read_artifact_text(self.data_root, artifact)
