from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from devauto.core.models import GateResult, ProjectConfig, Run
from devauto.runner.ai_cli import AiCliAdapter


REVIEWER_ROLES = ["reviewer_a", "reviewer_b", "reviewer_c"]


@dataclass(frozen=True)
class ReviewDecision:
    decision: str
    feedback: str
    reviewer_outputs: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.decision == "pass"


class ReviewHarness:
    def __init__(self, project: ProjectConfig) -> None:
        self.project = project

    def review(
        self,
        run: Run,
        workspace: Path,
        plan_doc: str,
        diff: str,
        gate_results: list[GateResult],
    ) -> ReviewDecision:
        roles = REVIEWER_ROLES[: max(1, min(run.review_depth, len(REVIEWER_ROLES)))]
        configured_roles = [role for role in roles if self.project.ai.get(role) and self.project.ai[role].command]
        if not configured_roles:
            return ReviewDecision(
                decision="pass",
                feedback="reviewer AI command가 설정되지 않았습니다. Deterministic review fallback을 통과했습니다.",
                reviewer_outputs={},
            )

        ai = AiCliAdapter(self.project)
        outputs: dict[str, str] = {}
        decisions: list[str] = []
        feedback: list[str] = []
        prompt = build_review_prompt(run, plan_doc, diff, gate_results)
        for role in configured_roles:
            result = ai.run(role, workspace, prompt)
            outputs[role] = result.output
            if not result.ok:
                decisions.append("escalate")
                feedback.append(f"{role}가 exit code {result.exit_code}로 실패했습니다.\n{result.output}")
                continue
            decision = parse_decision(result.output)
            decisions.append(decision)
            if decision != "pass":
                feedback.append(f"{role}가 {decision}를 요청했습니다.\n{result.output}")

        if "escalate" in decisions:
            return ReviewDecision("escalate", "\n\n".join(feedback), outputs)
        if "fix" in decisions:
            return ReviewDecision("fix", "\n\n".join(feedback), outputs)
        return ReviewDecision("pass", "Reviewer AI 검사를 통과했습니다.", outputs)


def parse_decision(output: str) -> str:
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "decision":
            decision = value.strip().lower()
            if decision in {"pass", "fix", "escalate"}:
                return decision
    return "pass"


def build_review_prompt(run: Run, plan_doc: str, diff: str, gate_results: list[GateResult]) -> str:
    gate_summary = "\n".join(
        f"- {result.gate_name}: exit={result.exit_code}, duration_ms={result.duration_ms}, log={result.log_path.name}"
        for result in gate_results
    )
    return f"""당신은 reviewer AI입니다.
승인된 Plan Doc, final diff, deterministic gate 결과를 검토하세요.
파일을 수정하지 마세요.
아래 decision line 중 정확히 하나를 반환하세요:
DECISION: pass
DECISION: fix
DECISION: escalate

executor가 제한된 수정을 해야 하면 `fix`를 사용하세요.
문제가 안전하지 않거나, 불명확하거나, 승인된 scope 밖이면 `escalate`를 사용하세요.

실행: {run.id}
세션: {run.session_id}
대상 브랜치: {run.target_branch}
변경 등급: {run.change_class or "알 수 없음"}
리뷰 깊이: {run.review_depth}
작업: {run.title}

Plan Doc:
{plan_doc}

Gate 결과:
{gate_summary}

Diff:
```diff
{diff[:12000]}
```
"""
