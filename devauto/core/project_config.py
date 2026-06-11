from __future__ import annotations

from pathlib import Path

import yaml

from devauto.core.models import ProjectConfig


def load_project_config(path: Path) -> ProjectConfig:
    return parse_project_config(path.read_text(encoding="utf-8"), config_path=str(path.expanduser().resolve()))


def parse_project_config(text: str, config_path: str = "") -> ProjectConfig:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("project config YAML은 mapping이어야 합니다")
    if config_path and not data.get("config_path"):
        data = {**data, "config_path": config_path}
    return ProjectConfig.from_mapping(data)
