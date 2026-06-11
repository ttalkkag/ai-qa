from __future__ import annotations

from pathlib import Path

from devauto.core.models import ProjectConfig, Run
from devauto.core.policy import forbidden_path_matches
from devauto.runner.subprocesses import run_args


CONTEXT_SNIPPET_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "CONTRIBUTING.md",
    "docs/README.md",
]
MAX_CONTEXT_SNIPPET_CHARS = 12000
MAX_CONTEXT_FILE_CHARS = 4000


def collect_context(workspace: Path, run: Run, project: ProjectConfig) -> str:
    status = run_args(["git", "status", "--short"], cwd=workspace).combined_output
    log = run_args(["git", "log", "--oneline", "-20"], cwd=workspace).combined_output
    files = "\n".join(iter_workspace_files(workspace, project, max_depth=3))
    snippets = format_context_snippets(collect_context_snippets(workspace, project))
    return f"""# Context Packet

## 요청
- project: {project.id}
- session_id: {run.session_id}
- title: {run.title}
- target_branch: {run.target_branch}
- mode: {run.mode}

{run.description}

## Git Status
```text
{status}
```

## 최근 Git 이력
```text
{log}
```

## 파일 트리 (depth <= 3)
```text
{files}
```

## 프로젝트 컨텍스트 스니펫
{snippets}
"""


def iter_workspace_files(workspace: Path, project: ProjectConfig, max_depth: int) -> list[str]:
    paths: list[str] = []
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace)
        if ".git" in relative.parts:
            continue
        if len(relative.parts) > max_depth:
            continue
        relative_text = relative.as_posix()
        if path.is_file() and forbidden_path_matches(relative_text, project.policy) is None:
            paths.append(relative_text)
    return sorted(paths)


def collect_context_snippets(
    workspace: Path,
    project: ProjectConfig,
    candidates: list[str] | None = None,
) -> list[tuple[str, str]]:
    snippets: list[tuple[str, str]] = []
    remaining = MAX_CONTEXT_SNIPPET_CHARS
    for relative_text in candidates or CONTEXT_SNIPPET_FILES:
        if remaining <= 0:
            break
        if forbidden_path_matches(relative_text, project.policy) is not None:
            continue
        path = workspace / relative_text
        if not path.is_file() or not path.resolve().is_relative_to(workspace.resolve()):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not content:
            continue
        limit = min(MAX_CONTEXT_FILE_CHARS, remaining)
        if len(content) > limit:
            content = f"{content[:limit]}\n[잘림]"
        snippets.append((relative_text, content))
        remaining -= len(content)
    return snippets


def format_context_snippets(snippets: list[tuple[str, str]]) -> str:
    if not snippets:
        return "표준 프로젝트 컨텍스트 파일을 찾지 못했습니다."
    sections = []
    for path, content in snippets:
        sections.append(
            f"""### {path}
```text
{content}
```"""
        )
    return "\n\n".join(sections)
