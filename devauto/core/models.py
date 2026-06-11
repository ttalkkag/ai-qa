from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import AddressValueError, IPv4Address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from devauto.core.policy import command_contains_inline_secret, command_is_allowed, output_command_is_allowed


class RunStatus(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    PLAN_REVIEWED = "PLAN_REVIEWED"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    PROVISIONING = "PROVISIONING"
    EXECUTING = "EXECUTING"
    DETERMINISTIC_CHECKS = "DETERMINISTIC_CHECKS"
    AI_REVIEWING = "AI_REVIEWING"
    FIXING = "FIXING"
    AWAITING_QA_APPROVAL = "AWAITING_QA_APPROVAL"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    AWAITING_PUBLISH_APPROVAL = "AWAITING_PUBLISH_APPROVAL"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = {
    RunStatus.PUBLISHED,
    RunStatus.ESCALATED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


@dataclass(frozen=True)
class AiRoleConfig:
    command: list[str] = field(default_factory=list)
    timeout_sec: int = 900


@dataclass(frozen=True)
class DockerConfig:
    enabled: bool = True
    compose_files: list[str] = field(default_factory=list)
    project_name_prefix: str = "devauto"
    preview_service: str = "app"
    preview_container_port: int = 3000
    host_bind_ip: str = "127.0.0.1"
    env_file: str | None = None


@dataclass(frozen=True)
class PolicyConfig:
    default_mode: str = "human-reviewed"
    max_inner_gate_fixes: int = 2
    max_outer_ai_fixes: int = 2
    forbidden_paths: list[str] = field(default_factory=lambda: [".env", ".env.*", "secrets/**"])
    high_risk_paths: list[str] = field(default_factory=list)
    forbidden_commands: list[str] = field(
        default_factory=lambda: ["sudo", "docker system prune", "rm -rf /", "git commit", "git push", "ssh ", "deploy"]
    )


@dataclass(frozen=True)
class PublishConfig:
    mode: str = "patch_only"
    require_qa_approval: bool = False
    require_human_approval: bool = False
    commit_message_template: str = "chore: devauto run ${RUN_ID}"
    deploy_command: str = ""
    deploy_timeout_sec: int = 900


@dataclass(frozen=True)
class WorkspaceConfig:
    keep_success_runs: int = 10
    keep_failed_runs: int = 30


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DOCKER_PROJECT_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RUN_MODES = {"human-reviewed", "auto"}
PUBLISH_MODES = {"patch_only", "local_branch", "push_branch", "deploy_command"}
GIT_REF_FORBIDDEN_CHARS = set(" ~^:?*[\\")
RUN_TITLE_MAX_LENGTH = 200
RUN_DESCRIPTION_MAX_LENGTH = 20000
BLOCKED_INLINE_SECRET_COMMAND = "blocked --token [REDACTED]"
BLOCKED_INLINE_SECRET_ARGS = ("blocked", "--token", "[REDACTED]")


def validate_project_id(value: object) -> str:
    project_id = str(value or "").strip()
    if not project_id:
        raise ValueError("project id가 필요합니다")
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ValueError("project id는 1-128자의 영문자, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다")
    return project_id


def validate_config_key(value: object, field_name: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError(f"{field_name}이 필요합니다")
    if not CONFIG_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"{field_name}은 1-64자의 영문자, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다")
    return key


def validate_optional_mapping(value: object, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{field_name}은 mapping이어야 합니다")


def validate_list(value: object, field_name: str, default: list[object] | None = None) -> list[object]:
    if value is None:
        return list(default or [])
    if isinstance(value, list):
        return value
    raise ValueError(f"{field_name}은 list여야 합니다")


def validate_repo_url(value: object) -> str:
    repo_url = str(value or "").strip()
    if not repo_url:
        return ""
    parsed = urlsplit(repo_url)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"} and (parsed.username or parsed.password):
        raise ValueError("repo.url에는 credential을 포함할 수 없습니다. git credential manager나 SSH agent를 사용하세요")
    if parsed.password:
        raise ValueError("repo.url에는 credential을 포함할 수 없습니다. git credential manager나 SSH agent를 사용하세요")
    return repo_url


def validate_command_has_no_inline_secret(value: object, field_name: str, inline_secret_action: str = "reject") -> str:
    command = str(value or "")
    if command_contains_inline_secret(command):
        if inline_secret_action == "redact":
            return BLOCKED_INLINE_SECRET_COMMAND
        raise ValueError(
            f"{field_name}에는 inline secret을 포함할 수 없습니다. credential manager, "
            "Docker env_file 또는 사전 인증된 CLI를 사용하세요"
        )
    return command


def validate_workspace_relative_path(value: object, field_name: str) -> str:
    path = str(value or "").strip()
    if not path:
        raise ValueError(f"{field_name}이 필요합니다")
    if path.startswith("~") or path.startswith(("/", "\\")):
        raise ValueError(f"{field_name}은 project workspace 기준 상대 경로여야 합니다")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError(f"{field_name}에 제어 문자가 포함되어 있습니다")
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"{field_name}은 project workspace 밖으로 벗어날 수 없습니다")
    return normalized


def validate_docker_env_file(value: object) -> str | None:
    if value is None:
        return None
    path = str(value or "").strip()
    if not path:
        return None
    if path.startswith("~") or Path(path).is_absolute():
        return path
    return validate_workspace_relative_path(path, "docker.env_file")


def validate_docker_project_prefix(value: object) -> str:
    prefix = str(value or "").strip()
    if not prefix:
        raise ValueError("docker.project_name_prefix가 필요합니다")
    if not DOCKER_PROJECT_PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError("docker.project_name_prefix는 소문자, 숫자, 밑줄, 하이픈만 사용할 수 있습니다")
    return prefix


def validate_port(value: object, field_name: str) -> int:
    port = validate_integer(value, field_name)
    if port < 1 or port > 65535:
        raise ValueError(f"{field_name}은 1부터 65535 사이여야 합니다")
    return port


def validate_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name}은 integer여야 합니다")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
    raise ValueError(f"{field_name}은 integer여야 합니다")


def validate_non_negative_int(value: object, field_name: str) -> int:
    integer = validate_integer(value, field_name)
    if integer < 0:
        raise ValueError(f"{field_name}은 0 이상의 integer여야 합니다")
    return integer


def validate_positive_int(value: object, field_name: str) -> int:
    integer = validate_integer(value, field_name)
    if integer < 1:
        raise ValueError(f"{field_name}은 0보다 커야 합니다")
    return integer


def validate_docker_host_bind_ip(value: object) -> str:
    ip = str(value or "").strip()
    if not ip:
        raise ValueError("docker.host_bind_ip가 필요합니다")
    try:
        return str(IPv4Address(ip))
    except AddressValueError as exc:
        raise ValueError("docker.host_bind_ip는 127.0.0.1 또는 0.0.0.0 같은 IPv4 주소여야 합니다") from exc


def validate_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{field_name}은 boolean이어야 합니다")


def validate_run_mode(value: object) -> str:
    mode = str(value or "").strip()
    if mode not in RUN_MODES:
        allowed = ", ".join(sorted(RUN_MODES))
        raise ValueError(f"run mode는 다음 중 하나여야 합니다: {allowed}")
    return mode


def validate_publish_mode(value: object) -> str:
    mode = str(value or "").strip()
    if mode not in PUBLISH_MODES:
        allowed = ", ".join(sorted(PUBLISH_MODES))
        raise ValueError(f"publish.mode는 다음 중 하나여야 합니다: {allowed}")
    return mode


def validate_run_title(value: object) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise ValueError("run title이 필요합니다")
    if len(title) > RUN_TITLE_MAX_LENGTH:
        raise ValueError(f"run title은 {RUN_TITLE_MAX_LENGTH}자 이하여야 합니다")
    if contains_disallowed_control_characters(title):
        raise ValueError("run title에 제어 문자가 포함되어 있습니다")
    return title


def normalize_run_description(value: object) -> str:
    description = str(value or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if len(description) > RUN_DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"run description은 {RUN_DESCRIPTION_MAX_LENGTH}자 이하여야 합니다")
    if contains_disallowed_control_characters(description, allow_newlines=True):
        raise ValueError("run description에 제어 문자가 포함되어 있습니다")
    return description


def contains_disallowed_control_characters(value: str, allow_newlines: bool = False) -> bool:
    allowed = {"\n", "\t"} if allow_newlines else set()
    return any((ord(char) < 32 or ord(char) == 127) and char not in allowed for char in value)


def validate_git_branch(value: object, field_name: str = "branch") -> str:
    branch = str(value or "").strip()
    if not branch:
        raise ValueError(f"{field_name}이 필요합니다")
    reason = _git_ref_rejection_reason(branch)
    if reason:
        raise ValueError(f"{field_name}은 안전한 git branch name이어야 합니다: {reason}")
    return branch


def validate_git_branch_prefix(value: object) -> str:
    prefix = str(value or "").strip()
    if not prefix:
        raise ValueError("repo.branch_prefix가 필요합니다")
    candidate = f"{prefix}branch"
    reason = _git_ref_rejection_reason(candidate)
    if reason:
        raise ValueError(f"repo.branch_prefix는 안전한 git branch name을 만들어야 합니다: {reason}")
    return prefix


def _git_ref_rejection_reason(value: str) -> str | None:
    if len(value) > 255:
        return "너무 깁니다"
    if value.startswith("-"):
        return "'-'로 시작할 수 없습니다"
    if value.startswith("/") or value.endswith("/") or value.endswith("."):
        return "'/'로 시작하거나 끝날 수 없고 '.'로 끝날 수 없습니다"
    if value == "@":
        return "'@'일 수 없습니다"
    if ".." in value or "@{" in value or "//" in value:
        return "'..', '@{', '//'를 포함할 수 없습니다"
    if any(ord(char) < 32 or ord(char) == 127 or char in GIT_REF_FORBIDDEN_CHARS for char in value):
        return "git ref에서 허용되지 않는 문자를 포함합니다"
    for part in value.split("/"):
        if not part:
            return "빈 path segment를 포함합니다"
        if part.startswith("."):
            return "path segment는 '.'로 시작할 수 없습니다"
        if part.endswith(".lock"):
            return "path segment는 '.lock'으로 끝날 수 없습니다"
    return None


@dataclass(frozen=True)
class ProjectConfig:
    id: str
    name: str
    repo_url: str
    config_path: str = ""
    default_branch: str = "main"
    branch_prefix: str = "aiqa/"
    commands: dict[str, str] = field(default_factory=dict)
    ai: dict[str, AiRoleConfig] = field(default_factory=dict)
    docker: DockerConfig = field(default_factory=DockerConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], inline_secret_action: str = "reject") -> "ProjectConfig":
        project_id = validate_project_id(data.get("id"))
        repo = validate_optional_mapping(data.get("repo"), "repo")
        policy_data = validate_optional_mapping(data.get("policy"), "policy")
        default_forbidden_commands = [
            "sudo",
            "docker system prune",
            "rm -rf /",
            "git commit",
            "git push",
            "ssh ",
            "deploy",
        ]
        policy = PolicyConfig(
            default_mode=validate_run_mode(policy_data.get("default_mode", "human-reviewed")),
            max_inner_gate_fixes=validate_non_negative_int(
                policy_data.get("max_inner_gate_fixes", 2),
                "policy.max_inner_gate_fixes",
            ),
            max_outer_ai_fixes=validate_non_negative_int(
                policy_data.get("max_outer_ai_fixes", 2),
                "policy.max_outer_ai_fixes",
            ),
            forbidden_paths=[
                str(item)
                for item in validate_list(
                    policy_data.get("forbidden_paths"),
                    "policy.forbidden_paths",
                    [".env", ".env.*", "secrets/**"],
                )
            ],
            high_risk_paths=[
                str(item) for item in validate_list(policy_data.get("high_risk_paths"), "policy.high_risk_paths")
            ],
            forbidden_commands=[
                str(item)
                for item in validate_list(
                    policy_data.get("forbidden_commands"),
                    "policy.forbidden_commands",
                    default_forbidden_commands,
                )
            ],
        )
        ai_data = validate_optional_mapping(data.get("ai"), "ai")
        ai = {}
        for name, value in ai_data.items():
            role = validate_config_key(name, "ai role name")
            role_data = validate_optional_mapping(value, f"ai.{role}")
            role_command = [
                str(item) for item in validate_list(role_data.get("command"), f"ai.{role}.command")
            ]
            checked_command = validate_command_has_no_inline_secret(
                " ".join(role_command),
                f"ai.{role}.command",
                inline_secret_action,
            )
            if checked_command == BLOCKED_INLINE_SECRET_COMMAND:
                role_command = list(BLOCKED_INLINE_SECRET_ARGS)
            elif checked_command and inline_secret_action != "redact" and not command_is_allowed(checked_command, policy):
                raise ValueError(f"ai.{role}.command에 금지된 command가 포함되어 있습니다")
            ai[role] = AiRoleConfig(
                command=role_command,
                timeout_sec=validate_positive_int(role_data.get("timeout_sec", 900), f"ai.{role}.timeout_sec"),
            )
        docker_data = validate_optional_mapping(data.get("docker"), "docker")
        publish_data = validate_optional_mapping(data.get("publish"), "publish")
        workspace_data = validate_optional_mapping(data.get("workspace"), "workspace")
        commands_data = validate_optional_mapping(data.get("commands"), "commands")
        commands = {}
        for key, value in commands_data.items():
            name = validate_config_key(key, "command name")
            if not isinstance(value, str):
                raise ValueError(f"commands.{name}은 string이어야 합니다")
            if value:
                checked_command = validate_command_has_no_inline_secret(
                    value,
                    f"commands.{name}",
                    inline_secret_action,
                )
                if checked_command != BLOCKED_INLINE_SECRET_COMMAND and inline_secret_action != "redact":
                    if not command_is_allowed(checked_command, policy):
                        raise ValueError(f"commands.{name}에 금지된 command가 포함되어 있습니다")
                commands[name] = checked_command
        deploy_command = validate_command_has_no_inline_secret(
            publish_data.get("deploy_command") or "",
            "publish.deploy_command",
            inline_secret_action,
        )
        if deploy_command and deploy_command != BLOCKED_INLINE_SECRET_COMMAND and inline_secret_action != "redact":
            if not output_command_is_allowed(deploy_command, policy):
                raise ValueError("publish.deploy_command에 금지된 command가 포함되어 있습니다")
        return cls(
            id=project_id,
            name=str(data.get("name") or project_id),
            config_path=str(data.get("config_path") or ""),
            repo_url=validate_repo_url(repo.get("url") or data.get("repo_url") or ""),
            default_branch=validate_git_branch(
                repo.get("default_branch") or data.get("default_branch") or "main",
                "repo.default_branch",
            ),
            branch_prefix=validate_git_branch_prefix(repo.get("branch_prefix") or "aiqa/"),
            commands=commands,
            ai=ai,
            docker=DockerConfig(
                enabled=validate_bool(docker_data.get("enabled", True), "docker.enabled"),
                compose_files=[
                    validate_workspace_relative_path(item, "docker.compose_files")
                    for item in validate_list(docker_data.get("compose_files"), "docker.compose_files")
                ],
                project_name_prefix=validate_docker_project_prefix(
                    docker_data.get("project_name_prefix", "devauto")
                ),
                preview_service=validate_config_key(docker_data.get("preview_service", "app"), "docker.preview_service"),
                preview_container_port=validate_port(
                    docker_data.get("preview_container_port", 3000),
                    "docker.preview_container_port",
                ),
                host_bind_ip=validate_docker_host_bind_ip(docker_data.get("host_bind_ip", "127.0.0.1")),
                env_file=validate_docker_env_file(docker_data.get("env_file")),
            ),
            policy=policy,
            publish=PublishConfig(
                mode=validate_publish_mode(publish_data.get("mode", "patch_only")),
                require_qa_approval=validate_bool(
                    publish_data.get("require_qa_approval", False),
                    "publish.require_qa_approval",
                ),
                require_human_approval=validate_bool(
                    publish_data.get("require_human_approval", False),
                    "publish.require_human_approval",
                ),
                commit_message_template=str(
                    publish_data.get("commit_message_template", "chore: devauto run ${RUN_ID}")
                ),
                deploy_command=deploy_command,
                deploy_timeout_sec=validate_positive_int(
                    publish_data.get("deploy_timeout_sec", 900),
                    "publish.deploy_timeout_sec",
                ),
            ),
            workspace=WorkspaceConfig(
                keep_success_runs=validate_non_negative_int(
                    workspace_data.get("keep_success_runs", 10),
                    "workspace.keep_success_runs",
                ),
                keep_failed_runs=validate_non_negative_int(
                    workspace_data.get("keep_failed_runs", 30),
                    "workspace.keep_failed_runs",
                ),
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "config_path": self.config_path,
            "repo": {
                "url": self.repo_url,
                "default_branch": self.default_branch,
                "branch_prefix": self.branch_prefix,
            },
            "commands": self.commands,
            "ai": {
                role: {"command": config.command, "timeout_sec": config.timeout_sec}
                for role, config in self.ai.items()
            },
            "docker": {
                "enabled": self.docker.enabled,
                "compose_files": self.docker.compose_files,
                "project_name_prefix": self.docker.project_name_prefix,
                "preview_service": self.docker.preview_service,
                "preview_container_port": self.docker.preview_container_port,
                "host_bind_ip": self.docker.host_bind_ip,
                "env_file": self.docker.env_file,
            },
            "policy": {
                "default_mode": self.policy.default_mode,
                "max_inner_gate_fixes": self.policy.max_inner_gate_fixes,
                "max_outer_ai_fixes": self.policy.max_outer_ai_fixes,
                "forbidden_paths": self.policy.forbidden_paths,
                "high_risk_paths": self.policy.high_risk_paths,
                "forbidden_commands": self.policy.forbidden_commands,
            },
            "publish": {
                "mode": self.publish.mode,
                "require_qa_approval": self.publish.require_qa_approval,
                "require_human_approval": self.publish.require_human_approval,
                "commit_message_template": self.publish.commit_message_template,
                "deploy_command": self.publish.deploy_command,
                "deploy_timeout_sec": self.publish.deploy_timeout_sec,
            },
            "workspace": {
                "keep_success_runs": self.workspace.keep_success_runs,
                "keep_failed_runs": self.workspace.keep_failed_runs,
            },
        }


@dataclass(frozen=True)
class Run:
    id: str
    session_id: str
    project_id: str
    title: str
    description: str
    target_branch: str
    mode: str
    status: RunStatus
    request: dict[str, Any]
    change_class: str | None = None
    review_depth: int = 1
    workspace_path: Path | None = None
    preview_url: str | None = None
    current_retry: int = 0
    max_retries: int = 2
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Artifact:
    id: int
    run_id: str
    kind: str
    name: str
    path: Path
    created_at: str


@dataclass(frozen=True)
class Approval:
    id: int
    run_id: str
    approval_type: str
    decision: str
    comment: str
    created_at: str


@dataclass(frozen=True)
class GateResult:
    gate_name: str
    command: str
    exit_code: int
    log_path: Path
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0
