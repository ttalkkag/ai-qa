from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_root: Path
    database_path: Path
    bind_host: str = "127.0.0.1"
    bind_port: int = 7700
    preview_host: str = "127.0.0.1"
    preview_base_port: int = 18080
    preview_port_count: int = 256
    shared_token: str | None = None


def load_settings(data_root: Path | None = None) -> Settings:
    root = (data_root or Path(os.environ.get("DEVAUTO_HOME", ".devauto"))).expanduser().resolve()
    bind_host = os.environ.get("DEVAUTO_BIND_HOST", "127.0.0.1")
    bind_port = env_int("DEVAUTO_BIND_PORT", "7700")
    validate_port_env("DEVAUTO_BIND_PORT", bind_port)
    preview_host = os.environ.get("DEVAUTO_PREVIEW_HOST", bind_host)
    preview_base_port = env_int("DEVAUTO_PREVIEW_BASE_PORT", "18080")
    validate_port_env("DEVAUTO_PREVIEW_BASE_PORT", preview_base_port)
    preview_port_count = env_int("DEVAUTO_PREVIEW_PORT_COUNT", "256")
    if preview_port_count <= 0:
        raise ValueError("DEVAUTO_PREVIEW_PORT_COUNT는 0보다 커야 합니다")
    preview_port_end = preview_base_port + preview_port_count - 1
    if preview_port_end > 65535:
        raise ValueError("DEVAUTO_PREVIEW_BASE_PORT + DEVAUTO_PREVIEW_PORT_COUNT - 1은 65535 이하여야 합니다")
    shared_token = os.environ.get("DEVAUTO_SHARED_TOKEN") or None
    return Settings(
        data_root=root,
        database_path=root / "data" / "devauto.sqlite3",
        bind_host=bind_host,
        bind_port=bind_port,
        preview_host=preview_host,
        preview_base_port=preview_base_port,
        preview_port_count=preview_port_count,
        shared_token=shared_token,
    )


def env_int(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}은 integer여야 합니다") from exc


def validate_port_env(name: str, port: int) -> None:
    if port < 1 or port > 65535:
        raise ValueError(f"{name}은 1부터 65535 사이여야 합니다")
