from __future__ import annotations

import re
import shlex
from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devauto.core.models import PolicyConfig


HARNESS_FORBIDDEN_COMMANDS = [
    "sudo",
    "docker system prune",
    "rm -rf /",
    "git commit",
    "git push",
    "ssh ",
    "deploy",
]

OUTPUT_FORBIDDEN_COMMANDS = [
    "sudo",
    "docker system prune",
    "rm -rf /",
    "git commit",
    "git push",
    "ssh ",
]


HIGH_RISK_WORDS = {
    "auth",
    "authentication",
    "authorization",
    "billing",
    "payment",
    "migration",
    "secret",
    "infra",
    "deploy",
    "delete",
    "remove",
}

SECRET_KEY_NAME = (
    r"(?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd)"
)

INLINE_SECRET_PATTERNS = [
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/?#\s@]+@"),
    re.compile(r"(?i)\b(?:authorization|set-cookie|cookie)\s*[:=]\s*[^\r\n\s]+"),
    re.compile(rf"(?i)(?<![A-Za-z0-9_-])--?{SECRET_KEY_NAME}(?:\s+|=)[^\s,;}}]+"),
    re.compile(rf"(?i)(?<![A-Za-z0-9]){SECRET_KEY_NAME}\s*[:=]\s*[\"']?[^\"'\s,;}}]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
]


def classify_change(text: str, policy: PolicyConfig, candidate_files: list[str] | None = None) -> tuple[str, int]:
    lowered = text.lower()
    if any(word in lowered for word in HIGH_RISK_WORDS):
        return "high-risk", 3
    if any(high_risk_path_matches(path, policy) for path in candidate_files or []):
        return "high-risk", 3
    if any(pattern.lower().strip("*") in lowered for pattern in policy.high_risk_paths):
        return "high-risk", 3
    if "docs" in lowered or "readme" in lowered:
        return "small", 1
    return "standard", 2


def high_risk_path_matches(path: str, policy: PolicyConfig) -> str | None:
    normalized = path.strip("/")
    for pattern in policy.high_risk_paths:
        if fnmatch(normalized, pattern.strip("/")):
            return pattern
    return None


def effective_forbidden_commands(policy: PolicyConfig) -> list[str]:
    commands: list[str] = []
    for command in [*HARNESS_FORBIDDEN_COMMANDS, *policy.forbidden_commands]:
        if command not in commands:
            commands.append(command)
    return commands


def command_is_allowed(command: str, policy: PolicyConfig) -> bool:
    if command_contains_inline_secret(command):
        return False
    return not any(command_matches_forbidden(command, forbidden) for forbidden in effective_forbidden_commands(policy))


def output_command_is_allowed(command: str, policy: PolicyConfig) -> bool:
    if command_contains_inline_secret(command):
        return False
    project_forbidden = [item for item in policy.forbidden_commands if item.strip().lower() != "deploy"]
    return not any(command_matches_forbidden(command, forbidden) for forbidden in [*OUTPUT_FORBIDDEN_COMMANDS, *project_forbidden])


def command_matches_forbidden(command: str, forbidden: str) -> bool:
    phrase = forbidden.strip().casefold()
    if not phrase:
        return False
    if phrase in {"git commit", "git push"}:
        return command_has_git_subcommand(command, phrase.split()[1])
    command_text = command.casefold()
    if phrase == "deploy":
        return phrase in command_text
    tokens = phrase.split()
    if not tokens:
        return False
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    first = tokens[0]
    last = tokens[-1]
    if first and (first[0].isalnum() or first[0] == "_"):
        pattern = rf"(?<![A-Za-z0-9_.-]){pattern}"
    if last and (last[-1].isalnum() or last[-1] == "_"):
        pattern = rf"{pattern}(?![A-Za-z0-9_.-])"
    return re.search(pattern, command_text) is not None


def command_has_git_subcommand(command: str, subcommand: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    target = subcommand.casefold()
    for index, token in enumerate(tokens):
        if token.casefold() != "git":
            continue
        actual = git_subcommand(tokens[index + 1 :])
        if actual == target:
            return True
    return False


def git_subcommand(tokens: list[str]) -> str | None:
    index = 0
    options_with_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            continue
        if token in options_with_value:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in options_with_value if option.startswith("--")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.casefold()
    return None


def command_contains_inline_secret(command: str) -> bool:
    return any(pattern.search(command) for pattern in INLINE_SECRET_PATTERNS)


def forbidden_path_matches(path: str, policy: PolicyConfig) -> str | None:
    normalized = path.strip("/")
    for pattern in policy.forbidden_paths:
        if fnmatch(normalized, pattern.strip("/")):
            return pattern
    return None


def find_forbidden_paths(paths: list[str], policy: PolicyConfig) -> list[tuple[str, str]]:
    return [(path, pattern) for path in paths if (pattern := forbidden_path_matches(path, policy))]
