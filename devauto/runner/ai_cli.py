from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from devauto.core.models import AiRoleConfig, ProjectConfig, Run
from devauto.runner.subprocesses import run_args, sanitized_child_env


PLAN_REQUIRED_SECTIONS = [
    "Context Summary",
    "Change Goal",
    "Candidate Files",
    "Test Scenarios",
    "Edge Cases",
    "Open Questions",
]

BARE_CANDIDATE_FILES = {
    "containerfile",
    "dockerfile",
    "gemfile",
    "justfile",
    "license",
    "licence",
    "makefile",
    "procfile",
    "rakefile",
    "readme",
    "taskfile",
}


@dataclass(frozen=True)
class AiCliResult:
    role: str
    skipped: bool
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class AiCliAdapter:
    def __init__(self, project: ProjectConfig) -> None:
        self.project = project

    def run(self, role: str, workspace: Path, prompt: str) -> AiCliResult:
        config = self.project.ai.get(role, AiRoleConfig())
        if not config.command:
            return AiCliResult(role=role, skipped=True, exit_code=0, output=f"{role} command가 설정되지 않았습니다.")
        result = run_args(
            config.command,
            cwd=workspace,
            timeout_sec=config.timeout_sec,
            input_text=prompt,
            env=sanitized_child_env(),
        )
        return AiCliResult(role=role, skipped=False, exit_code=result.exit_code, output=result.combined_output)


def validate_plan_doc(plan_doc: str) -> list[str]:
    seen = {_normalize_plan_section(line) for line in plan_doc.splitlines()}
    return [section for section in PLAN_REQUIRED_SECTIONS if section.casefold() not in seen]


def extract_candidate_files(plan_doc: str) -> list[str]:
    candidates: list[str] = []
    in_candidate_section = False
    for line in plan_doc.splitlines():
        section = _normalize_plan_section(line)
        if section == "candidate files":
            in_candidate_section = True
            continue
        if in_candidate_section and section:
            break
        if not in_candidate_section:
            continue
        for candidate in _candidate_paths_from_line(line):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _normalize_plan_section(line: str) -> str:
    text = line.strip()
    if not text:
        return ""
    if text.startswith("#"):
        text = text.lstrip("#").strip()
    text = _strip_ordered_prefix(text)
    for section in PLAN_REQUIRED_SECTIONS:
        if text.casefold() == section.casefold():
            return section.casefold()
    return ""


def _strip_ordered_prefix(text: str) -> str:
    index = 0
    while index < len(text) and text[index].isdigit():
        index += 1
    if index and index < len(text) and text[index] in {".", ")"}:
        return text[index + 1 :].strip()
    return text


def _candidate_paths_from_line(line: str) -> list[str]:
    text = _strip_candidate_line_prefix(line)
    if not text:
        return []

    quoted_paths = [
        candidate
        for value in re.findall(r"`([^`]+)`", text)
        if (candidate := _normalize_candidate_path(value, allow_bare=True))
    ]
    if quoted_paths:
        return quoted_paths

    if ":" in text:
        label, value = text.split(":", 1)
        if _is_candidate_label(label):
            text = value.strip()

    candidates: list[str] = []
    for chunk in re.split(r"[,;]", text):
        chunk = re.split(r"\s+--?\s+", chunk.strip(), maxsplit=1)[0]
        chunk = chunk.split(" (", 1)[0].strip()
        words = chunk.split()
        if not words:
            continue
        candidate = _normalize_candidate_path(words[0], allow_bare=len(words) == 1)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _strip_candidate_line_prefix(line: str) -> str:
    text = line.strip()
    text = re.sub(r"^[-*+]\s+", "", text)
    text = re.sub(r"^\d+[.)]\s+", "", text)
    return text.strip()


def _is_candidate_label(label: str) -> bool:
    normalized = label.strip().casefold().replace("_", " ").replace("-", " ")
    return normalized in {
        "allowed file",
        "allowed files",
        "candidate file",
        "candidate files",
        "file",
        "files",
        "path",
        "paths",
        "scope",
    }


def _normalize_candidate_path(raw: str, allow_bare: bool) -> str | None:
    path = raw.strip().strip("`'\"<>")
    while path and path[-1] in ".,;)":
        path = path[:-1]
    path = path.replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    if not path or path.casefold() in {"n/a", "na", "none", "tbd", "unknown"}:
        return None
    if any(char.isspace() for char in path) or "\x00" in path:
        return None
    if path.startswith(("/", "~", "../")) or "/../" in path or "://" in path or ":" in path:
        return None

    name = path.rsplit("/", 1)[-1]
    has_path_signal = (
        path.endswith("/")
        or "/" in path
        or any(char in path for char in "*?[]")
        or "." in name
        or name.casefold() in BARE_CANDIDATE_FILES
    )
    if not has_path_signal:
        return None
    if "/" not in path and "." not in name and name.casefold() not in BARE_CANDIDATE_FILES:
        return None
    if not allow_bare and not ("/" in path or "." in name or any(char in path for char in "*?[]")):
        return None
    return path


def build_plan_prompt(run: Run, project: ProjectConfig, context: str, planning_feedback: str = "") -> str:
    feedback = f"\n반영해야 할 human plan feedback:\n{planning_feedback}\n" if planning_feedback else ""
    return f"""당신은 Planner AI입니다.
코드를 수정하면 안 됩니다.
아래 정확한 섹션을 가진 Plan Doc을 작성하세요:
1. Context Summary
2. Change Goal
3. Candidate Files
4. Test Scenarios
5. Edge Cases
6. Open Questions

프로젝트: {project.id}
세션 ID: {run.session_id}
요청 제목: {run.title}
요청 본문:
{run.description}
{feedback}

Context packet:
{context}
"""


def fallback_plan_doc(
    run: Run,
    project: ProjectConfig,
    context: str,
    planning_feedback: str = "",
    fallback_reason: str = "Planner AI가 설정되지 않아 의도적으로 보수적인 fallback plan을 사용합니다.",
) -> str:
    feedback_section = (
        f"\n- 최근 human plan feedback: {planning_feedback}\n- 다음 executor run은 이 feedback을 반영해야 합니다.\n"
        if planning_feedback
        else ""
    )
    return f"""# Plan Doc

## 1. Context Summary
- Project `{project.id}`를 격리된 run workspace로 clone했습니다.
- git status, 최근 이력, 얕은 file tree에서 deterministic context를 수집했습니다.
- {fallback_reason}

## 2. Change Goal
- 요청된 변경: {run.title}
- 상세: {run.description or "추가 설명이 없습니다."}
- executor 단계에서 publish, push, deploy, secret 접근을 하지 않습니다.
{feedback_section}

## 3. Candidate Files
- Executor는 변경을 run workspace 안으로 제한해야 합니다.
- Executor는 요청에 직접 필요한 파일만 수정해야 합니다.
- Policy gate는 configured forbidden path를 차단합니다.

## 4. Test Scenarios
- configured deterministic gate를 순서대로 실행합니다.
- 각 gate log를 보존합니다.
- gate 실패가 configured retry limit을 넘으면 중단하고 escalate합니다.

## 5. Edge Cases
- 추가 scope가 필요하면 중단하고 plan update를 요청합니다.
- command 또는 Docker setup이 없으면 추측하지 말고 failure report로 드러냅니다.

## 6. Open Questions
- executor write access 전에 human approval이 필요합니다.

---

## Context Packet 스냅샷

{context[:4000]}
"""


def build_executor_prompt(run: Run, plan_doc: str, execution_grant: str) -> str:
    return f"""당신은 Executor AI입니다.
주어진 workspace 안의 파일만 수정할 수 있습니다.
승인된 Plan Doc을 따라야 합니다.
Execution Grant를 반드시 지켜야 합니다.
commit, push, deploy, secret 접근을 하지 마세요.
추가 파일이 필요하면 중단하고 scope expansion을 요청하세요.
변경 후에는 멈추세요. harness가 deterministic gate를 실행합니다.

실행: {run.id}
세션: {run.session_id}
작업: {run.title}

승인된 Plan Doc:
{plan_doc}

Execution Grant:
{execution_grant}
"""


def build_fix_prompt(run: Run, gate_name: str, command: str, log_excerpt: str, execution_grant: str) -> str:
    return f"""당신은 deterministic gate 실패를 수정하는 Executor AI입니다.
실패한 문제만 수정하세요.
scope를 확장하지 마세요.
Execution Grant를 반드시 지켜야 합니다.
commit, push, deploy, secret 접근을 하지 마세요.
수정 후에는 멈추세요. harness가 gate를 다시 실행합니다.

실행: {run.id}
세션: {run.session_id}
게이트: {gate_name}
명령: {command}

관련 log excerpt:
{log_excerpt}

Execution Grant:
{execution_grant}
"""


def build_review_fix_prompt(run: Run, feedback: str, execution_grant: str) -> str:
    return f"""당신은 reviewer feedback을 수정하는 Executor AI입니다.
reviewer가 명시적으로 요청한 문제만 수정하세요.
scope를 확장하지 마세요.
Execution Grant를 반드시 지켜야 합니다.
commit, push, deploy, secret 접근을 하지 마세요.
수정 후에는 멈추세요. harness가 gate와 review를 다시 실행합니다.

실행: {run.id}
세션: {run.session_id}
작업: {run.title}

Reviewer feedback:
{feedback}

Execution Grant:
{execution_grant}
"""
