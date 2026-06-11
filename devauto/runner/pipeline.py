from __future__ import annotations

import ast
import json
import shutil
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urlsplit

from devauto.core.config import Settings
from devauto.core.db import Database
from devauto.core.doctor import format_doctor_report, run_project_doctor
from devauto.core.models import Approval, Artifact, GateResult, ProjectConfig, Run, RunStatus, TERMINAL_STATUSES
from devauto.core.network import preview_lan_urls_for_settings, preview_url_for_settings
from devauto.core.policy import classify_change, effective_forbidden_commands, find_forbidden_paths
from devauto.runner.ai_cli import (
    AiCliAdapter,
    build_executor_prompt,
    build_fix_prompt,
    build_plan_prompt,
    build_review_fix_prompt,
    extract_candidate_files,
    fallback_plan_doc,
    validate_plan_doc,
)
from devauto.runner.artifacts import ArtifactStore, safe_artifact_path
from devauto.runner.context import collect_context
from devauto.runner.gates import DockerGateRunner
from devauto.runner.publisher import Publisher, PublishResult
from devauto.runner.reviewer import ReviewDecision, ReviewHarness, parse_decision
from devauto.runner.subprocesses import CommandResult, run_args, sanitized_child_env
from devauto.runner.workspace import (
    expected_workspace_path,
    path_contains_symlink,
    provision_workspace,
    workspace_display_path,
    workspace_path_rejection,
)


AUTO_PLAN_APPROVAL_COMMENT = "작고 유효한 Planner Plan Doc이라 run mode에 따라 자동 승인되었습니다."
STALE_RECOVERY_STATUSES = {
    RunStatus.PREPARING,
    RunStatus.PLAN_REVIEWED,
    RunStatus.PROVISIONING,
    RunStatus.EXECUTING,
    RunStatus.DETERMINISTIC_CHECKS,
    RunStatus.AI_REVIEWING,
    RunStatus.FIXING,
    RunStatus.READY_TO_PUBLISH,
    RunStatus.PUBLISHING,
}
CANCELLABLE_STATUSES = {
    RunStatus.RECEIVED,
    RunStatus.QUEUED,
    RunStatus.PREPARING,
    RunStatus.PLAN_REVIEWED,
    RunStatus.AWAITING_PLAN_APPROVAL,
    RunStatus.PROVISIONING,
    RunStatus.EXECUTING,
    RunStatus.FIXING,
    RunStatus.DETERMINISTIC_CHECKS,
    RunStatus.AI_REVIEWING,
    RunStatus.AWAITING_QA_APPROVAL,
    RunStatus.READY_TO_PUBLISH,
    RunStatus.AWAITING_PUBLISH_APPROVAL,
}


class Pipeline:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.artifacts = ArtifactStore(settings.data_root, db)

    def prepare_run_safely(self, run_id: str, planning_feedback: str = "") -> Run:
        try:
            return self.prepare_run(run_id, planning_feedback)
        except Exception as exc:
            return self.fail_run(run_id, f"예상하지 못한 준비 실패: {type(exc).__name__}: {exc}")

    def approve_plan_and_execute_safely(self, run_id: str, comment: str = "") -> Run:
        try:
            return self.approve_plan_and_execute(run_id, comment)
        except Exception as exc:
            return self.fail_run(run_id, f"예상하지 못한 실행 실패: {type(exc).__name__}: {exc}")

    def approve_publish_safely(self, run_id: str, comment: str = "") -> Run:
        try:
            return self.approve_publish(run_id, comment)
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            return self.fail_run(run_id, f"예상하지 못한 publish 실패: {type(exc).__name__}: {exc}")

    def approve_qa_preview_safely(self, run_id: str, comment: str = "") -> Run:
        try:
            return self.approve_qa_preview(run_id, comment)
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            return self.fail_run(run_id, f"예상하지 못한 QA 승인 실패: {type(exc).__name__}: {exc}")

    def prepare_run(self, run_id: str, planning_feedback: str = "", already_claimed: bool = False) -> Run:
        current = self.db.get_run(run_id)
        if already_claimed:
            if current.status != RunStatus.PREPARING:
                raise ValueError(f"prepare에는 PREPARING 상태가 필요합니다. 현재 상태: {current.status.value}")
        else:
            if current.status in {RunStatus.RECEIVED, RunStatus.QUEUED} and self.db.has_active_run(exclude_run_id=run_id):
                if current.status == RunStatus.RECEIVED:
                    return self.db.transition_run(run_id, RunStatus.QUEUED)
                return current
            claimed = self.db.try_transition_run(
                run_id,
                [RunStatus.RECEIVED, RunStatus.QUEUED, RunStatus.AWAITING_PLAN_APPROVAL],
                RunStatus.PREPARING,
            )
            if claimed is None:
                return self.db.get_run(run_id)
        run = self.db.get_run(run_id)
        project = self.db.get_project(run.project_id)
        if not self._artifact_exists(run_id, "00-request.json"):
            self.artifacts.write_text(
                run_id,
                "trace",
                "00-request.json",
                format_json_trace({"request": run.request, "run": run_trace(run, self.settings), "project": project_trace(project)}),
            )

        doctor = run_project_doctor(project, target_branch=run.target_branch)
        self.artifacts.write_text(run_id, "doctor", "00-doctor.md", format_doctor_report(doctor))
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        if doctor.status == "fail":
            return self.escalate(run_id, "project doctor가 실패했습니다. 00-doctor.md를 확인하세요")

        try:
            workspace = provision_workspace(self.settings.data_root, run, project)
        except Exception as exc:
            return self.escalate(run_id, f"workspace 준비 실패: {exc}")
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        self.db.update_run(run_id, workspace_path=workspace)
        run = self.db.get_run(run_id)

        context = collect_context(workspace, run, project)
        self.artifacts.write_text(run_id, "context", "00-context.md", context)
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled

        planner = AiCliAdapter(project)
        planner_status_before = self._workspace_status_lines(workspace)
        planner_result = planner.run("planner", workspace, build_plan_prompt(run, project, context, planning_feedback))
        self.artifacts.write_text(run_id, "planner_log", "01-planner.log", planner_result.output)
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        planner_status_after = self._workspace_status_lines(workspace)
        if planner_status_after != planner_status_before:
            self.artifacts.write_text(
                run_id,
                "planner_policy",
                "01-planner-write-violation.md",
                format_workspace_status_change(planner_status_before, planner_status_after),
            )
            return self.escalate(run_id, "planner가 plan 승인 전에 workspace를 변경했습니다")
        candidate_plan = (
            planner_result.output
            if planner_result.ok and not planner_result.skipped and planner_result.output.strip()
            else ""
        )
        missing_sections: list[str] = []
        used_fallback_plan = not bool(candidate_plan)
        if candidate_plan:
            missing_sections = validate_plan_doc(candidate_plan)
            used_fallback_plan = bool(missing_sections)
            if missing_sections:
                self.artifacts.write_text(
                    run_id,
                    "plan_policy",
                    "01-plan-invalid.md",
                    format_invalid_plan_doc(candidate_plan, missing_sections),
                )
        fallback_reason = "planner output이 비었거나 skip되었거나 사용할 수 없어 보수적인 fallback plan을 사용합니다."
        if missing_sections:
            fallback_reason = "planner output에 필수 Plan Doc 섹션이 없어 보수적인 fallback plan을 사용합니다."
        plan_doc = (
            fallback_plan_doc(run, project, context, planning_feedback, fallback_reason)
            if used_fallback_plan
            else candidate_plan
        )
        fallback_missing_sections = validate_plan_doc(plan_doc)
        if fallback_missing_sections:
            return self.escalate(
                run_id,
                "fallback Plan Doc 구조 검증 실패: "
                + ", ".join(fallback_missing_sections),
            )
        self.artifacts.write_text(run_id, "plan", "01-plan.md", plan_doc)

        candidate_files = extract_candidate_files(plan_doc)
        change_class, review_depth = classify_change(f"{run.title}\n{run.description}", project.policy, candidate_files)
        self.db.update_run(run_id, change_class=change_class, review_depth=review_depth)
        run = self.db.get_run(run_id)
        plan_review = format_plan_review(
            change_class,
            review_depth,
            used_fallback_plan,
            missing_sections,
            candidate_files,
        )
        plan_review_result, used_plan_reviewer = self._review_plan(run_id, run, project, workspace, plan_doc, plan_review)
        if plan_review_result is not None:
            return plan_review_result
        if not used_plan_reviewer:
            self.artifacts.write_text(run_id, "plan_review", "02-plan-review.md", plan_review)
        auto_decision = auto_plan_approval_decision(
            run,
            change_class,
            used_fallback_plan,
            missing_sections,
            planning_feedback,
        )
        if auto_decision is not None:
            approved, auto_policy = auto_decision
            self.artifacts.write_text(run_id, "auto_policy", "02-auto-policy.md", auto_policy)
            waiting = self.db.transition_run(run_id, RunStatus.AWAITING_PLAN_APPROVAL)
            if approved:
                return self.approve_plan_and_execute(run_id, AUTO_PLAN_APPROVAL_COMMENT)
            return waiting
        return self.db.transition_run(run_id, RunStatus.AWAITING_PLAN_APPROVAL)

    def approve_plan_and_execute(self, run_id: str, comment: str = "") -> Run:
        claimed = self.db.try_transition_run(
            run_id,
            [RunStatus.AWAITING_PLAN_APPROVAL],
            RunStatus.PROVISIONING,
        )
        if claimed is None:
            run = self.db.get_run(run_id)
            if run.status in {
                RunStatus.RECEIVED,
                RunStatus.QUEUED,
                RunStatus.PREPARING,
                RunStatus.PLAN_REVIEWED,
                RunStatus.AWAITING_PLAN_APPROVAL,
            }:
                raise ValueError(
                    "plan 승인에는 "
                    f"{RunStatus.AWAITING_PLAN_APPROVAL.value} 상태가 필요합니다. 현재 상태: {run.status.value}"
                )
            return run
        run = claimed
        plan_doc = self.artifacts.read_text(run_id, "01-plan.md")
        candidate_files = extract_candidate_files(plan_doc)
        project = self.db.get_project(run.project_id)
        if rejection := workspace_path_rejection(self.settings.data_root, run_id, run.workspace_path):
            return self.escalate(run_id, rejection)
        self.db.add_approval(run_id, "plan", "approved", comment)
        self.artifacts.write_text(
            run_id,
            "grant",
            "02-execution-grant.json",
            format_execution_grant(
                create_execution_grant(
                    run,
                    project,
                    comment,
                    source_repo_snapshot(project.repo_url, self.settings.data_root),
                    candidate_files,
                )
            ),
        )
        self.db.transition_run(run_id, RunStatus.EXECUTING)
        return self.execute_run(run_id)

    def reject_plan(self, run_id: str, comment: str = "") -> Run:
        claimed = self.db.try_transition_run(
            run_id,
            [RunStatus.AWAITING_PLAN_APPROVAL],
            RunStatus.PREPARING,
        )
        if claimed is None:
            run = self.db.get_run(run_id)
            if run.status == RunStatus.PREPARING:
                return run
            raise ValueError(
                f"plan 반려에는 {RunStatus.AWAITING_PLAN_APPROVAL.value} 상태가 필요합니다. "
                f"현재 상태: {run.status.value}"
            )
        self.db.add_approval(run_id, "plan", "rejected", comment)
        feedback = comment or "추가 코멘트 없이 Plan이 반려되었습니다."
        self.artifacts.write_text(
            run_id,
            "plan_rejection",
            "02-plan-rejection.md",
            f"# Plan 반려\n\n{feedback}\n",
        )
        return self.prepare_run(run_id, planning_feedback=feedback, already_claimed=True)

    def cancel_run(self, run_id: str) -> Run:
        current = self.db.get_run(run_id)
        run = self.db.try_transition_run(run_id, list(CANCELLABLE_STATUSES), RunStatus.CANCELLED)
        if run is None:
            latest = self.db.get_run(run_id)
            if latest.status in TERMINAL_STATUSES:
                return latest
            raise ValueError(f"cancel 가능한 상태가 필요합니다. 현재 상태: {latest.status.value}")
        self._finalize_cancelled_run(run_id, f"{current.status.value} 상태에서 run이 취소되었습니다.")
        if current.status != RunStatus.QUEUED:
            self.start_next_queued()
        return run

    def execute_run(self, run_id: str) -> Run:
        run = self.db.get_run(run_id)
        project = self.db.get_project(run.project_id)
        if run.workspace_path is None:
            raise RuntimeError("run에 workspace가 없습니다")
        if rejection := workspace_path_rejection(self.settings.data_root, run_id, run.workspace_path):
            return self.escalate(run_id, rejection)

        workspace = run.workspace_path
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        plan_doc = self.artifacts.read_text(run_id, "01-plan.md")
        try:
            execution_grant = self.artifacts.read_text(run_id, "02-execution-grant.json")
            grant = json.loads(execution_grant)
        except KeyError:
            return self.escalate(run_id, "execution grant가 없습니다. plan approval이 executor 접근을 승인하지 않았습니다")
        except json.JSONDecodeError as exc:
            return self.escalate(run_id, f"execution grant가 올바르지 않습니다: {exc}")
        ai = AiCliAdapter(project)

        executor_result = ai.run("executor", workspace, build_executor_prompt(run, plan_doc, execution_grant))
        self.artifacts.write_text(run_id, "execution", "03-execution.log", executor_result.output)
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        if not executor_result.ok:
            return self.escalate(run_id, f"executor가 exit code {executor_result.exit_code}로 실패했습니다")

        gate_fix_count = 0
        gate_run_count = 0
        review_fix_count = 0
        while True:
            gate_fix_attempt = 0
            while True:
                if cancelled := self._return_if_cancelled(run_id):
                    return cancelled
                source_violation = source_repo_violation(grant, self.settings.data_root)
                if source_violation:
                    return self.escalate(run_id, source_violation)

                forbidden = self._find_forbidden_diff_paths(workspace, project)
                if forbidden:
                    message = "\n".join(f"- {path} -> {pattern} 패턴과 일치" for path, pattern in forbidden)
                    return self.escalate(run_id, f"forbidden path가 변경되었습니다:\n{message}")

                scope_violations = self._find_scope_diff_paths(workspace, grant)
                if scope_violations:
                    self.artifacts.write_text(
                        run_id,
                        "scope_policy",
                        "02-scope-violation.md",
                        format_scope_violation(scope_allowed_files(grant), scope_violations),
                    )
                    return self.escalate(
                        run_id,
                        "변경 경로가 승인된 Plan Doc scope 밖입니다. 02-scope-violation.md를 확인하세요",
                    )

                diff = self._git_diff(workspace)
                self.artifacts.write_text(run_id, "diff", "07-diff.patch", diff)
                self.db.transition_run(run_id, RunStatus.DETERMINISTIC_CHECKS)

                gate_run_count += 1
                artifact_suffix = "" if gate_run_count == 1 else f"-{gate_run_count}"
                gate_runner = DockerGateRunner(
                    project,
                    self.artifacts,
                    run_id,
                    self._preview_port(run_id),
                    artifact_suffix=artifact_suffix,
                )
                try:
                    gate_run = gate_runner.run_all(workspace)
                except Exception as exc:
                    return self.escalate(run_id, f"gate runner 실패: {exc}")

                for result in gate_run.results:
                    self.db.add_gate_result(run_id, result)
                if cancelled := self._return_if_cancelled(run_id):
                    return cancelled

                source_violation = source_repo_violation(grant, self.settings.data_root)
                if source_violation:
                    return self.escalate(run_id, source_violation)

                scope_violations = self._find_scope_diff_paths(workspace, grant)
                if scope_violations:
                    self.artifacts.write_text(
                        run_id,
                        "scope_policy",
                        "02-scope-violation.md",
                        format_scope_violation(scope_allowed_files(grant), scope_violations),
                    )
                    return self.escalate(
                        run_id,
                        "변경 경로가 승인된 Plan Doc scope 밖입니다. 02-scope-violation.md를 확인하세요",
                    )

                if gate_run.ok:
                    break

                if gate_fix_attempt >= project.policy.max_inner_gate_fixes:
                    return self.escalate(run_id, "deterministic gate 실패가 허용 횟수를 초과했습니다")

                self.db.transition_run(run_id, RunStatus.FIXING)
                failed = gate_run.failed
                assert failed is not None
                log_excerpt = failed.log_path.read_text(encoding="utf-8")[-6000:]
                gate_fix_count += 1
                self.db.update_run(run_id, current_retry=gate_fix_count + review_fix_count)
                fix_result = ai.run(
                    "executor",
                    workspace,
                    build_fix_prompt(run, failed.gate_name, failed.command, log_excerpt, execution_grant),
                )
                self.artifacts.write_text(run_id, "execution", f"03-fix-{gate_fix_count}.log", fix_result.output)
                if cancelled := self._return_if_cancelled(run_id):
                    return cancelled
                if not fix_result.ok or fix_result.skipped:
                    return self.escalate(run_id, "gate가 실패했고 executor fix command를 사용할 수 없거나 실패했습니다")
                self.db.transition_run(run_id, RunStatus.EXECUTING)
                gate_fix_attempt += 1

            self.db.transition_run(run_id, RunStatus.AI_REVIEWING)
            if cancelled := self._return_if_cancelled(run_id):
                return cancelled
            reviewer_status_before = self._workspace_status_lines(workspace)
            review = ReviewHarness(project).review(run, workspace, plan_doc, diff, gate_run.results)
            self._write_review_artifacts(run, review)
            if cancelled := self._return_if_cancelled(run_id):
                return cancelled
            reviewer_status_after = self._workspace_status_lines(workspace)
            if reviewer_status_after != reviewer_status_before:
                self.artifacts.write_text(
                    run_id,
                    "review_policy",
                    "08-reviewer-write-violation.md",
                    format_workspace_status_change(
                        reviewer_status_before,
                        reviewer_status_after,
                        role="reviewer",
                        rule="deterministic gate 이후 workspace 파일을 변경하면 안 됩니다",
                    ),
                )
                return self.escalate(run_id, "reviewer가 deterministic gate 이후 workspace를 변경했습니다")
            source_violation = source_repo_violation(grant, self.settings.data_root)
            if source_violation:
                return self.escalate(run_id, source_violation)
            if review.ok:
                return self._finalize_success(run_id, project, workspace)
            if review.decision == "escalate":
                return self.escalate(run_id, review.feedback)
            if review_fix_count >= project.policy.max_outer_ai_fixes:
                return self.escalate(run_id, "AI review fix 허용 횟수를 초과했습니다")
            self.db.transition_run(run_id, RunStatus.FIXING)
            review_fix_count += 1
            self.db.update_run(run_id, current_retry=gate_fix_count + review_fix_count)
            fix_result = ai.run("executor", workspace, build_review_fix_prompt(run, review.feedback, execution_grant))
            self.artifacts.write_text(run_id, "execution", f"03-review-fix-{review_fix_count}.log", fix_result.output)
            if cancelled := self._return_if_cancelled(run_id):
                return cancelled
            if not fix_result.ok or fix_result.skipped:
                return self.escalate(run_id, "AI review가 fix를 요청했지만 executor fix command를 사용할 수 없거나 실패했습니다")
            self.db.transition_run(run_id, RunStatus.EXECUTING)

        return self.escalate(run_id, "도달할 수 없는 retry 상태입니다")

    def approve_publish(self, run_id: str, comment: str = "") -> Run:
        claimed = self.db.try_transition_run(
            run_id,
            [RunStatus.AWAITING_PUBLISH_APPROVAL],
            RunStatus.PUBLISHING,
        )
        if claimed is None:
            run = self.db.get_run(run_id)
            if run.status in {RunStatus.PUBLISHING, *TERMINAL_STATUSES}:
                return run
            raise ValueError(
                f"publish 승인에는 {RunStatus.AWAITING_PUBLISH_APPROVAL.value} 상태가 필요합니다. "
                f"현재 상태: {run.status.value}"
            )
        self.db.add_approval(run_id, "publish", "approved", comment)
        return self._publish_claimed_run(run_id)

    def reject_publish(self, run_id: str, comment: str = "") -> Run:
        claimed = self.db.try_transition_run(
            run_id,
            [RunStatus.AWAITING_PUBLISH_APPROVAL],
            RunStatus.ESCALATED,
        )
        if claimed is None:
            run = self.db.get_run(run_id)
            if run.status in TERMINAL_STATUSES:
                return run
            raise ValueError(
                f"publish 반려에는 {RunStatus.AWAITING_PUBLISH_APPROVAL.value} 상태가 필요합니다. "
                f"현재 상태: {run.status.value}"
            )
        self.db.add_approval(run_id, "publish", "rejected", comment)
        return self._finalize_claimed_escalation(run_id, f"Publish가 반려되었습니다: {comment or '코멘트 없음'}")

    def approve_qa_preview(self, run_id: str, comment: str = "") -> Run:
        run = self.db.get_run(run_id)
        project = self.db.get_project(run.project_id)
        target = (
            RunStatus.AWAITING_PUBLISH_APPROVAL
            if self._requires_publish_approval(run, project)
            else RunStatus.READY_TO_PUBLISH
        )
        claimed = self.db.try_transition_run(run_id, [RunStatus.AWAITING_QA_APPROVAL], target)
        if claimed is None:
            current = self.db.get_run(run_id)
            if current.status in {target, RunStatus.PUBLISHING, *TERMINAL_STATUSES}:
                return current
            raise ValueError(
                f"QA preview 승인에는 {RunStatus.AWAITING_QA_APPROVAL.value} 상태가 필요합니다. "
                f"현재 상태: {current.status.value}"
            )
        self.db.add_approval(run_id, "qa_preview", "approved", comment)
        if target == RunStatus.AWAITING_PUBLISH_APPROVAL:
            return claimed
        return self.publish_run(run_id)

    def reject_qa_preview(self, run_id: str, comment: str = "") -> Run:
        claimed = self.db.try_transition_run(
            run_id,
            [RunStatus.AWAITING_QA_APPROVAL],
            RunStatus.ESCALATED,
        )
        if claimed is None:
            run = self.db.get_run(run_id)
            if run.status in TERMINAL_STATUSES:
                return run
            raise ValueError(
                f"QA preview 반려에는 {RunStatus.AWAITING_QA_APPROVAL.value} 상태가 필요합니다. "
                f"현재 상태: {run.status.value}"
            )
        self.db.add_approval(run_id, "qa_preview", "rejected", comment)
        return self._finalize_claimed_escalation(run_id, f"QA preview가 반려되었습니다: {comment or '코멘트 없음'}")

    def publish_run(self, run_id: str) -> Run:
        claimed = self.db.try_transition_run(run_id, [RunStatus.READY_TO_PUBLISH], RunStatus.PUBLISHING)
        if claimed is None:
            run = self.db.get_run(run_id)
            if run.status in {RunStatus.PUBLISHING, *TERMINAL_STATUSES}:
                return run
            raise ValueError(
                f"publish에는 {RunStatus.READY_TO_PUBLISH.value} 상태가 필요합니다. 현재 상태: {run.status.value}"
            )
        return self._publish_claimed_run(run_id)

    def _publish_claimed_run(self, run_id: str) -> Run:
        run = self.db.get_run(run_id)
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        project = self.db.get_project(run.project_id)
        if run.workspace_path is None:
            return self.escalate(run_id, "publish할 workspace가 없습니다")
        if rejection := workspace_path_rejection(self.settings.data_root, run_id, run.workspace_path):
            return self.escalate(run_id, rejection)
        if source_violation := self._source_repo_violation_from_grant(run_id):
            return self.escalate(run_id, source_violation)

        try:
            result = Publisher().publish(run, project, run.workspace_path)
        except Exception as exc:
            return self.fail_run(run_id, f"예상하지 못한 publish 실패: {type(exc).__name__}: {exc}")
        self.artifacts.write_text(run_id, "publish", "12-publish.md", format_publish_result(result))
        if not result.ok:
            return self.escalate(run_id, result.summary)
        if source_violation := self._source_repo_violation_from_grant(run_id):
            return self.escalate(run_id, source_violation)
        published = self.db.transition_run(run_id, RunStatus.PUBLISHED)
        self._cleanup_preview(run_id)
        self._write_final_trace(run_id)
        self._prune_terminal_workspaces(project, run_id)
        self.start_next_queued()
        return published

    def escalate(self, run_id: str, reason: str) -> Run:
        run = self.db.get_run(run_id)
        diff, changed_paths = self._failure_context(run)
        failed_gate = self._latest_failed_gate(run_id)
        failing_log = self._failing_log_excerpt(run_id, failed_gate)
        workspace_display = workspace_display_path(self.settings.data_root, run_id, run.workspace_path) or "N/A"
        self.artifacts.write_text(
            run_id,
            "failure_report",
            "11-final-report.md",
            format_failure_report(run, reason, diff, changed_paths, failed_gate, failing_log, workspace_display),
        )
        escalated = self.db.transition_run(run_id, RunStatus.ESCALATED)
        self._cleanup_preview(run_id)
        project = self.db.get_project(run.project_id)
        self._write_final_trace(run_id)
        self._prune_terminal_workspaces(project, run_id)
        self.start_next_queued()
        return escalated

    def fail_run(self, run_id: str, reason: str) -> Run:
        run = self.db.get_run(run_id)
        if run.status in TERMINAL_STATUSES:
            return run
        diff, changed_paths = self._failure_context(run)
        failed_gate = self._latest_failed_gate(run_id)
        failing_log = self._failing_log_excerpt(run_id, failed_gate)
        workspace_display = workspace_display_path(self.settings.data_root, run_id, run.workspace_path) or "N/A"
        self.artifacts.write_text(
            run_id,
            "failure_report",
            "11-final-report.md",
            format_failure_report(run, reason, diff, changed_paths, failed_gate, failing_log, workspace_display),
        )
        failed = self.db.transition_run(run_id, RunStatus.FAILED)
        self._cleanup_preview(run_id)
        project = self.db.get_project(run.project_id)
        self._write_final_trace(run_id)
        self._prune_terminal_workspaces(project, run_id)
        self.start_next_queued()
        return failed

    def _finalize_claimed_escalation(self, run_id: str, reason: str) -> Run:
        run = self.db.get_run(run_id)
        diff, changed_paths = self._failure_context(run)
        failed_gate = self._latest_failed_gate(run_id)
        failing_log = self._failing_log_excerpt(run_id, failed_gate)
        workspace_display = workspace_display_path(self.settings.data_root, run_id, run.workspace_path) or "N/A"
        self.artifacts.write_text(
            run_id,
            "failure_report",
            "11-final-report.md",
            format_failure_report(run, reason, diff, changed_paths, failed_gate, failing_log, workspace_display),
        )
        self._cleanup_preview(run_id)
        project = self.db.get_project(run.project_id)
        self._write_final_trace(run_id)
        self._prune_terminal_workspaces(project, run_id)
        self.start_next_queued()
        return self.db.get_run(run_id)

    def recover_stale_runs(self, older_than_seconds: float) -> list[Run]:
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds는 0 이상이어야 합니다")
        now = datetime.now(UTC)
        recovered: list[Run] = []
        for run in sorted(self.db.list_runs(), key=lambda item: (item.updated_at, item.id)):
            if run.status not in STALE_RECOVERY_STATUSES:
                continue
            updated_at = parse_timestamp(run.updated_at)
            if updated_at is None:
                reason = f"updated_at 값이 올바르지 않아 stale recovery가 run을 실패 처리했습니다: {run.updated_at}"
                recovered.append(self.fail_run(run.id, reason))
                continue
            age_seconds = (now - updated_at).total_seconds()
            if age_seconds < older_than_seconds:
                continue
            reason = (
                "상태 갱신이 없어 stale active run을 복구했습니다: "
                f"{int(age_seconds)}초 동안 갱신 없음, 이전 updated_at={run.updated_at}"
            )
            recovered.append(self.fail_run(run.id, reason))
        return recovered

    def start_next_queued(self) -> Run | None:
        if self.db.has_active_run():
            return None
        next_run = self.db.next_queued_run()
        if next_run is None:
            return None
        return self.prepare_run_safely(next_run.id)

    def _finalize_success(self, run_id: str, project: ProjectConfig, workspace: Path) -> Run:
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        diff = self._git_diff(workspace)
        self.artifacts.write_text(run_id, "patch", "10-final.patch", diff)
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        preview_url = None
        preview_lan_urls: list[str] = []
        if project.publish.require_qa_approval and self._uses_compose_preview(project):
            preview_port = self._preview_port(run_id)
            preview_url = preview_url_for_settings(self.settings, preview_port)
            preview_lan_urls = preview_lan_urls_for_settings(self.settings, preview_port)
            gate_runner = DockerGateRunner(project, self.artifacts, run_id, preview_port)
            if not self._artifact_exists(run_id, "docker-compose-up.log"):
                try:
                    gate_runner.compose_up(workspace)
                except Exception as exc:
                    return self.escalate(run_id, f"preview 시작 실패: {exc}")
            try:
                gate_runner.compose_health_check(workspace)
            except Exception as exc:
                return self.escalate(run_id, f"preview health check 실패: {exc}")
        self.artifacts.write_text(
            run_id,
            "final_report",
            "11-final-report.md",
            f"""# 최종 보고서

## 결과
Deterministic gate를 통과했습니다.

## 미리보기
{preview_url or "Docker preview가 설정되지 않았습니다."}

## 미리보기 LAN URL
{format_preview_lan_urls(preview_lan_urls)}

## Publish 모드
{project.publish.mode}

## 패치
`10-final.patch`를 확인하세요.
""",
        )
        self.db.update_run(run_id, preview_url=preview_url)
        if project.publish.require_qa_approval:
            if preview_url is None:
                return self.escalate(run_id, "QA approval에는 preview URL이 필요하지만 Docker preview가 비활성화되어 있습니다")
            return self.db.transition_run(run_id, RunStatus.AWAITING_QA_APPROVAL)
        return self._advance_after_qa(run_id)

    def _advance_after_qa(self, run_id: str) -> Run:
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled
        run = self.db.get_run(run_id)
        project = self.db.get_project(run.project_id)
        if self._requires_publish_approval(run, project):
            if run.preview_url is None:
                self._cleanup_preview(run_id)
            return self.db.transition_run(run_id, RunStatus.AWAITING_PUBLISH_APPROVAL)
        self.db.transition_run(run_id, RunStatus.READY_TO_PUBLISH)
        return self.publish_run(run_id)

    def _requires_publish_approval(self, run: Run, project: ProjectConfig) -> bool:
        approval_only_modes = {"push_branch", "deploy_command"}
        return project.publish.require_human_approval or project.publish.mode in approval_only_modes or run.change_class == "high-risk"

    def _prune_terminal_workspaces(self, project: ProjectConfig, current_run_id: str) -> None:
        success_runs: list[Run] = []
        failed_runs: list[Run] = []
        for run in self.db.list_project_runs(project.id):
            if run.status == RunStatus.PUBLISHED:
                success_runs.append(run)
            elif run.status in {RunStatus.ESCALATED, RunStatus.FAILED, RunStatus.CANCELLED}:
                failed_runs.append(run)

        removed: list[str] = []
        errors: list[str] = []
        for run in success_runs[project.workspace.keep_success_runs :]:
            self._remove_workspace(run, removed, errors)
        for run in failed_runs[project.workspace.keep_failed_runs :]:
            self._remove_workspace(run, removed, errors)

        if removed or errors:
            self.artifacts.write_text(
                current_run_id,
                "retention",
                "13-retention.md",
                format_retention_report(project, removed, errors),
            )

    def _remove_workspace(self, run: Run, removed: list[str], errors: list[str]) -> None:
        workspace = run.workspace_path
        if workspace is None or not workspace.exists():
            return
        rejection = self._workspace_prune_rejection(run, workspace)
        if rejection:
            errors.append(f"{run.id}: {workspace} - {rejection}")
            return
        target = expected_workspace_path(self.settings.data_root, run.id)
        try:
            shutil.rmtree(target)
            removed.append(f"{run.id}: {target}")
        except OSError as exc:
            errors.append(f"{run.id}: {target} - {exc}")

    def _workspace_prune_rejection(self, run: Run, workspace: Path) -> str | None:
        actual = workspace.expanduser().resolve(strict=False)
        expected = expected_workspace_path(self.settings.data_root, run.id)
        expected_resolved = expected.resolve(strict=False)
        if actual != expected_resolved:
            return "run data root 밖 workspace는 정리하지 않습니다"
        if path_contains_symlink(expected, self.settings.data_root.absolute()):
            return "symlink가 포함된 workspace path는 정리하지 않습니다"
        if not expected.is_dir():
            return "디렉터리가 아닌 workspace는 정리하지 않습니다"
        return None

    def _git_diff(self, workspace: Path) -> str:
        untracked = self._untracked_paths(workspace)
        if untracked:
            run_pipeline_git(["git", "add", "-N", "--", *untracked], cwd=workspace)
        return run_pipeline_git(["git", "diff", "--binary"], cwd=workspace).combined_output

    def _find_forbidden_diff_paths(self, workspace: Path, project: ProjectConfig) -> list[tuple[str, str]]:
        return find_forbidden_paths(self._changed_paths(workspace), project.policy)

    def _find_scope_diff_paths(self, workspace: Path, grant: dict[str, object]) -> list[str]:
        allowed_files = scope_allowed_files(grant)
        if not allowed_files:
            return []
        return [
            path
            for path in self._diff_changed_paths(workspace)
            if not any(scope_path_matches(path, allowed) for allowed in allowed_files)
        ]

    def _source_repo_violation_from_grant(self, run_id: str) -> str | None:
        try:
            grant = json.loads(self.artifacts.read_text(run_id, "02-execution-grant.json"))
        except KeyError:
            return "execution grant가 없습니다. 승인된 plan에서 publish가 허가되지 않았습니다"
        except json.JSONDecodeError as exc:
            return f"execution grant가 올바르지 않습니다: {exc}"
        return source_repo_violation(grant, self.settings.data_root)

    def _diff_changed_paths(self, workspace: Path) -> list[str]:
        output = run_pipeline_git(["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace).combined_output
        return status_output_paths(output)

    def _changed_paths(self, workspace: Path) -> list[str]:
        output = "\n".join(self._workspace_status_lines(workspace))
        return status_output_paths(output)

    def _failure_context(self, run: Run) -> tuple[str, list[str]]:
        if run.workspace_path is None:
            return "", []
        if rejection := workspace_path_rejection(self.settings.data_root, run.id, run.workspace_path):
            return (
                f"실패 후 diff를 수집할 수 없습니다: {rejection}\n",
                [f"변경 경로를 수집할 수 없습니다: {rejection}"],
            )
        try:
            diff = self._git_diff(run.workspace_path)
        except Exception as exc:
            diff = f"실패 후 diff를 수집할 수 없습니다: {type(exc).__name__}: {exc}\n"
        try:
            changed_paths = self._changed_paths(run.workspace_path)
        except Exception as exc:
            changed_paths = [f"변경 경로를 수집할 수 없습니다: {type(exc).__name__}: {exc}"]
        return diff, changed_paths

    def _untracked_paths(self, workspace: Path) -> list[str]:
        output = run_pipeline_git(["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace).combined_output
        return [line[3:].strip() for line in output.splitlines() if line.startswith("?? ")]

    def _workspace_status_lines(self, workspace: Path) -> list[str]:
        output = run_pipeline_git(
            ["git", "status", "--ignored", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
        ).combined_output
        return [line for line in output.splitlines() if line]

    def _review_plan(
        self,
        run_id: str,
        run: Run,
        project: ProjectConfig,
        workspace: Path,
        plan_doc: str,
        deterministic_review: str,
    ) -> tuple[Run | None, bool]:
        role = "plan_reviewer"
        config = project.ai.get(role)
        if not config or not config.command:
            return None, False

        reviewer = AiCliAdapter(project)
        status_before = self._workspace_status_lines(workspace)
        result = reviewer.run(role, workspace, build_plan_review_prompt(run, plan_doc, deterministic_review))
        self.artifacts.write_text(run_id, "plan_review_ai", "02-plan-review-ai.md", result.output)
        if cancelled := self._return_if_cancelled(run_id):
            return cancelled, True
        status_after = self._workspace_status_lines(workspace)
        if status_after != status_before:
            self.artifacts.write_text(
                run_id,
                "plan_review_policy",
                "02-plan-review-write-violation.md",
                format_workspace_status_change(
                    status_before,
                    status_after,
                    role="plan reviewer",
                    rule="plan 승인 전에 workspace 파일을 변경하면 안 됩니다",
                ),
            )
            self.artifacts.write_text(
                run_id,
                "plan_review",
                "02-plan-review.md",
                format_plan_review_ai_result(deterministic_review, role, "escalate", result.output),
            )
            return self.escalate(run_id, "plan reviewer가 plan 승인 전에 workspace를 변경했습니다"), True
        if not result.ok:
            self.artifacts.write_text(
                run_id,
                "plan_review",
                "02-plan-review.md",
                format_plan_review_ai_result(deterministic_review, role, "escalate", result.output),
            )
            return self.escalate(run_id, f"plan reviewer가 exit code {result.exit_code}로 실패했습니다"), True
        decision = parse_decision(result.output)
        self.artifacts.write_text(
            run_id,
            "plan_review",
            "02-plan-review.md",
            format_plan_review_ai_result(deterministic_review, role, decision, result.output),
        )
        if decision != "pass":
            return self.escalate(run_id, f"plan reviewer가 {decision}를 요청했습니다. 02-plan-review.md를 확인하세요"), True
        return None, True

    def _preview_port(self, run_id: str) -> int:
        suffix_text = run_id.rsplit("-", 1)[-1]
        try:
            suffix = int(suffix_text, 16)
        except ValueError:
            suffix = 0
        return self.settings.preview_base_port + (suffix % self.settings.preview_port_count)

    def _write_review_artifacts(self, run: Run, review: ReviewDecision) -> None:
        header = format_review_artifact_header(run)
        if review.reviewer_outputs:
            for role, output in review.reviewer_outputs.items():
                self.artifacts.write_text(run.id, "review", f"08-{role}.md", f"{header}\n\n{output}")
        else:
            self.artifacts.write_text(run.id, "review", "08-review-fallback.md", f"{header}\n\n{review.feedback}")
        self.artifacts.write_text(
            run.id,
            "judge",
            "09-judge.md",
            f"{header}\n\n# Judge 결과\n\nDecision: {review.decision}\n\n## 피드백\n{review.feedback}\n",
        )

    def _cleanup_preview(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        project = self.db.get_project(run.project_id)
        if run.workspace_path is None or not self._uses_compose_preview(project):
            return
        if workspace_path_rejection(self.settings.data_root, run_id, run.workspace_path):
            return
        try:
            DockerGateRunner(project, self.artifacts, run_id, self._preview_port(run_id)).compose_down(run.workspace_path)
        except Exception as exc:
            self.artifacts.write_text(
                run_id,
                "gate",
                "docker-compose-down.log",
                f"미리보기 정리 실패: {exc}\n",
            )

    def _uses_compose_preview(self, project: ProjectConfig) -> bool:
        return project.docker.enabled and bool(project.docker.compose_files)

    def _artifact_exists(self, run_id: str, name: str) -> bool:
        try:
            self.db.get_artifact(run_id, name)
        except KeyError:
            return False
        return True

    def _write_final_trace(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        project = self.db.get_project(run.project_id)
        self.artifacts.write_text(
            run_id,
            "trace",
            "13-run-final.json",
            format_json_trace(
                {
                    "request": run.request,
                    "run": run_trace(run, self.settings),
                    "project": project_trace(project),
                    "artifacts": [
                        artifact_trace(artifact, self.settings.data_root)
                        for artifact in self.db.list_artifacts(run_id)
                        if artifact.name != "13-run-final.json"
                    ],
                    "approvals": [approval_trace(approval) for approval in self.db.list_approvals(run_id)],
                    "gates": [gate_trace(result, self.settings.data_root, run_id) for result in self.db.list_gate_results(run_id)],
                }
            ),
        )

    def _latest_failed_gate(self, run_id: str) -> GateResult | None:
        failed = [result for result in self.db.list_gate_results(run_id) if not result.ok]
        return failed[-1] if failed else None

    def _failing_log_excerpt(self, run_id: str, failed_gate: GateResult | None) -> str:
        if failed_gate is None:
            return "N/A"
        try:
            path = safe_artifact_path(self.settings.data_root, run_id, failed_gate.log_path)
            return path.read_text(encoding="utf-8")[-6000:] or "N/A"
        except (OSError, ValueError) as exc:
            return f"실패 로그를 읽을 수 없습니다: {exc}"

    def _return_if_cancelled(self, run_id: str) -> Run | None:
        run = self.db.get_run(run_id)
        if run.status != RunStatus.CANCELLED:
            return None
        self._finalize_cancelled_run(run_id, "실행 경계에서 run cancellation을 감지했습니다.")
        return run

    def _finalize_cancelled_run(self, run_id: str, reason: str) -> None:
        run = self.db.get_run(run_id)
        diff, changed_paths = self._failure_context(run)
        if not self._artifact_exists(run_id, "11-final-report.md"):
            self.artifacts.write_text(
                run_id,
                "cancellation_report",
                "11-final-report.md",
                format_cancellation_report(run, reason, diff, changed_paths),
            )
        self._cleanup_preview(run_id)
        project = self.db.get_project(run.project_id)
        if not self._artifact_exists(run_id, "13-run-final.json"):
            self._write_final_trace(run_id)
        self._prune_terminal_workspaces(project, run_id)

    def _require_status(self, run_id: str, expected: RunStatus, action: str) -> Run:
        run = self.db.get_run(run_id)
        if run.status != expected:
            raise ValueError(f"{action}에는 {expected.value} 상태가 필요합니다. 현재 상태: {run.status.value}")
        return run


def format_publish_result(result: PublishResult) -> str:
    return f"""# Publish 결과

## 모드
{result.mode}

## 결과
{"성공" if result.ok else "실패"}

## 요약
{result.summary}

## 명령
{result.command or "N/A"}

## 브랜치
{result.branch or "N/A"}

## 커밋
{result.commit_hash or "N/A"}
"""


def format_review_artifact_header(run: Run) -> str:
    return f"""# Review 추적

- run_id: {run.id}
- session_id: {run.session_id}
- target_branch: {run.target_branch}
- change_class: {run.change_class or "알 수 없음"}
- review_depth: {run.review_depth}"""


def format_preview_lan_urls(urls: list[str]) -> str:
    if not urls:
        return "N/A"
    return "\n".join(f"- {url}" for url in urls)


def parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_failure_report(
    run: Run,
    reason: str,
    diff: str,
    changed_paths: list[str],
    failed_gate: GateResult | None,
    failing_log: str,
    workspace_path: str,
) -> str:
    failed_gate_name = failed_gate.gate_name if failed_gate else "해당 없음"
    failed_gate_command = failed_gate.command if failed_gate else "해당 없음"
    changed = "\n".join(f"- {path}" for path in changed_paths) if changed_paths else "- 해당 없음"
    return f"""# 실패 보고서

## 요청
{run.title}

{run.description}

## 현재 상태
- 상태: {run.status.value}
- 재시도: {run.current_retry}/{run.max_retries}
- 실패한 게이트: {failed_gate_name}

## 사유
{reason}

## 변경된 경로
{changed}

## 실패 로그
게이트 command: `{failed_gate_command}`

```text
{failing_log}
```

## 추정 원인
{reason}

## 수동 다음 단계
실패한 gate log와 아래 patch를 확인하세요. workspace `{workspace_path}`가 아직 존재하면 그 위치에서 이어서 처리할 수 있습니다.

## 패치
```diff
{diff}
```
"""


def format_cancellation_report(run: Run, reason: str, diff: str, changed_paths: list[str]) -> str:
    changed = "\n".join(f"- {path}" for path in changed_paths) if changed_paths else "- 해당 없음"
    return f"""# 취소 보고서

## 요청
{run.title}

{run.description}

## 현재 상태
- 상태: {run.status.value}
- 재시도: {run.current_retry}/{run.max_retries}

## 사유
{reason}

## 취소 전 변경된 경로
{changed}

## 패치
```diff
{diff}
```

## 다음 단계
부분 작업 workspace를 확인하고 요청을 계속 진행해야 하면 새 run을 생성하세요.
"""


def format_plan_review(
    change_class: str,
    review_depth: int,
    used_fallback_plan: bool,
    missing_sections: list[str],
    candidate_files: list[str] | None = None,
) -> str:
    planner_status = "fallback Plan Doc 사용" if used_fallback_plan else "planner Plan Doc 승인 가능"
    if missing_sections:
        fallback_reason = "planner output에 필수 섹션이 없습니다: " + ", ".join(missing_sections)
    elif used_fallback_plan:
        fallback_reason = "planner output이 비었거나 skip되었거나 사용할 수 없습니다"
    else:
        fallback_reason = "필요 없음"
    candidates = ", ".join(candidate_files or []) or "없음"
    return f"""# Plan 검토

Deterministic Plan Doc 구조 검토를 통과했습니다.

- 필수 섹션: Context Summary, Change Goal, Candidate Files, Test Scenarios, Edge Cases, Open Questions
- planner 상태: {planner_status}
- fallback 사유: {fallback_reason}
- candidate files: {candidates}
- 변경 등급: {change_class}
- 리뷰 깊이: {review_depth}
"""


def build_plan_review_prompt(run: Run, plan_doc: str, deterministic_review: str) -> str:
    return f"""당신은 read-only Plan Reviewer AI입니다.
human approval과 executor write access 전에 Plan Doc을 검토하세요.
파일을 수정하지 마세요.
아래 decision line 중 정확히 하나를 반환하세요:
DECISION: pass
DECISION: fix
DECISION: escalate

Plan Doc에 필수 scope, test, edge case, human question이 빠졌으면 `fix`를 사용하세요.
요청이나 plan이 안전하지 않거나, 불명확하거나, project policy 밖이면 `escalate`를 사용하세요.

Run: {run.id}
Session: {run.session_id}
Task: {run.title}

요청:
{run.description or "추가 설명이 없습니다."}

Deterministic Plan Review:
{deterministic_review}

Plan Doc:
{plan_doc}
"""


def format_plan_review_ai_result(base_review: str, role: str, decision: str, output: str) -> str:
    return f"""{base_review}

## AI Plan Review

- role: {role}
- decision: {decision}

```text
{output[:6000]}
```
"""


def auto_plan_approval_decision(
    run: Run,
    change_class: str,
    used_fallback_plan: bool,
    missing_sections: list[str],
    planning_feedback: str,
) -> tuple[bool, str] | None:
    if run.mode != "auto":
        return None
    if planning_feedback.strip():
        return False, format_auto_plan_approval(
            False,
            "human rejection 뒤 Plan이 재생성되었으므로 human approval이 필요합니다.",
            change_class,
            used_fallback_plan,
            missing_sections,
        )
    if used_fallback_plan:
        reason = "Fallback Plan Doc은 human approval이 필요합니다."
        if missing_sections:
            reason = "Planner Plan Doc에 필수 섹션이 없어 fallback Plan Doc에 human approval이 필요합니다."
        return False, format_auto_plan_approval(
            False,
            reason,
            change_class,
            used_fallback_plan,
            missing_sections,
        )
    if change_class != "small":
        return False, format_auto_plan_approval(
            False,
            f"change_class가 {change_class}입니다. small 변경만 자동 승인할 수 있습니다.",
            change_class,
            used_fallback_plan,
            missing_sections,
        )
    return True, format_auto_plan_approval(
        True,
        "승인 가능한 Planner Plan Doc이 small로 분류되었습니다.",
        change_class,
        used_fallback_plan,
        missing_sections,
    )


def format_auto_plan_approval(
    approved: bool,
    reason: str,
    change_class: str,
    used_fallback_plan: bool,
    missing_sections: list[str],
) -> str:
    missing = ", ".join(missing_sections) if missing_sections else "없음"
    decision = "자동 승인" if approved else "수동 승인 필요"
    planner_status = "fallback Plan Doc 사용" if used_fallback_plan else "planner Plan Doc 승인 가능"
    return f"""# Auto Plan 승인

- 모드: auto
- 결정: {decision}
- 사유: {reason}
- 변경 등급: {change_class}
- planner 상태: {planner_status}
- 누락 섹션: {missing}
"""


def format_invalid_plan_doc(plan_doc: str, missing_sections: list[str]) -> str:
    missing = "\n".join(f"- {section}" for section in missing_sections)
    return f"""# 유효하지 않은 Plan Doc

Planner output에 모든 필수 Plan Doc 섹션이 포함되지 않아 승인하지 않았습니다.

## 누락 섹션
{missing}

## 복구
Deterministic fallback Plan Doc을 `01-plan.md`에 작성했습니다.

## Planner 출력
```text
{plan_doc}
```
"""


def create_execution_grant(
    run: Run,
    project: ProjectConfig,
    approval_comment: str = "",
    source_snapshot: dict[str, object] | None = None,
    candidate_files: list[str] | None = None,
) -> dict[str, object]:
    allowed_files = candidate_files or []
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "project_id": project.id,
        "workspace": str(run.workspace_path or ""),
        "target_branch": run.target_branch,
        "change_class": run.change_class,
        "review_depth": run.review_depth,
        "approval": {
            "type": "plan",
            "decision": "approved",
            "comment": approval_comment,
        },
        "scope": {
            "source": "approved Plan Doc",
            "plan_artifact": "01-plan.md",
            "workspace_only": True,
            "allowed_files": allowed_files,
            "scope_expansion": "stop and request approval before modifying additional files",
        },
        "allowed_commands": project.commands,
        "forbidden_paths": project.policy.forbidden_paths,
        "forbidden_commands": effective_forbidden_commands(project.policy),
        "limits": {
            "max_gate_fix_attempts": project.policy.max_inner_gate_fixes,
            "max_review_fix_attempts": project.policy.max_outer_ai_fixes,
        },
        "source_repo_snapshot": source_snapshot or source_repo_snapshot(project.repo_url, Path.cwd()),
        "publish_allowed": False,
    }


def format_execution_grant(grant: dict[str, object]) -> str:
    return json.dumps(grant, indent=2, sort_keys=True)


def format_json_trace(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def run_trace(run: Run, settings: Settings | None = None) -> dict[str, object]:
    preview_lan_urls: list[str] = []
    if settings is not None and run.preview_url:
        try:
            port = urlsplit(run.preview_url).port
        except ValueError:
            port = None
        if port is not None:
            preview_lan_urls = preview_lan_urls_for_settings(settings, port)
    return {
        "id": run.id,
        "session_id": run.session_id,
        "project_id": run.project_id,
        "title": run.title,
        "description": run.description,
        "target_branch": run.target_branch,
        "mode": run.mode,
        "status": run.status.value,
        "change_class": run.change_class,
        "review_depth": run.review_depth,
        "workspace_path": workspace_display_path(settings.data_root, run.id, run.workspace_path)
        if settings
        else (str(run.workspace_path) if run.workspace_path else None),
        "preview_url": run.preview_url,
        "preview_lan_urls": preview_lan_urls,
        "current_retry": run.current_retry,
        "max_retries": run.max_retries,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def project_trace(project: ProjectConfig) -> dict[str, object]:
    return {
        "id": project.id,
        "name": project.name,
        "repo_url": project.repo_url,
        "default_branch": project.default_branch,
        "branch_prefix": project.branch_prefix,
        "publish": {
            "mode": project.publish.mode,
            "require_qa_approval": project.publish.require_qa_approval,
            "require_human_approval": project.publish.require_human_approval,
            "deploy_command": project.publish.deploy_command,
            "deploy_timeout_sec": project.publish.deploy_timeout_sec,
        },
        "policy": {
            "default_mode": project.policy.default_mode,
            "max_inner_gate_fixes": project.policy.max_inner_gate_fixes,
            "max_outer_ai_fixes": project.policy.max_outer_ai_fixes,
            "forbidden_paths": project.policy.forbidden_paths,
            "high_risk_paths": project.policy.high_risk_paths,
        },
    }


def artifact_trace(artifact: Artifact, data_root: Path) -> dict[str, object]:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "name": artifact.name,
        "path": trace_confined_path(data_root, artifact.run_id, artifact.path),
        "created_at": artifact.created_at,
    }


def approval_trace(approval: Approval) -> dict[str, object]:
    return {
        "id": approval.id,
        "approval_type": approval.approval_type,
        "decision": approval.decision,
        "comment": approval.comment,
        "created_at": approval.created_at,
    }


def gate_trace(result: GateResult, data_root: Path, run_id: str) -> dict[str, object]:
    return {
        "gate_name": result.gate_name,
        "command": result.command,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "log_path": trace_confined_path(data_root, run_id, result.log_path),
    }


def trace_confined_path(data_root: Path, run_id: str, path: Path) -> str:
    try:
        return str(safe_artifact_path(data_root, run_id, path))
    except ValueError:
        return "사용 불가"


def format_retention_report(project: ProjectConfig, removed: list[str], errors: list[str]) -> str:
    removed_text = "\n".join(f"- {item}" for item in removed) if removed else "- 없음"
    errors_text = "\n".join(f"- {item}" for item in errors) if errors else "- 없음"
    return f"""# 작업공간 보존 정리

project retention policy에 따라 오래된 terminal run workspace를 정리했습니다.
산출물과 database row는 보존했습니다.

## 정책
- keep_success_runs: {project.workspace.keep_success_runs}
- keep_failed_runs: {project.workspace.keep_failed_runs}

## 제거된 작업공간
{removed_text}

## 오류
{errors_text}
"""


def format_scope_violation(allowed_files: list[str], violations: list[str]) -> str:
    allowed = "\n".join(f"- {path}" for path in allowed_files) if allowed_files else "- 구체 경로 없음"
    changed = "\n".join(f"- {path}" for path in violations) if violations else "- N/A"
    return f"""# Plan Scope 위반

executor가 승인된 Plan Doc Candidate Files scope 밖의 파일을 변경했습니다.

## 승인된 Candidate Files
{allowed}

## Scope 밖 변경 경로
{changed}

## 복구
이 run을 반려하거나 scope 확장을 명시한 새 Plan Doc을 요청하세요.
"""


def scope_allowed_files(grant: dict[str, object]) -> list[str]:
    scope = grant.get("scope")
    if not isinstance(scope, dict):
        return []
    allowed = scope.get("allowed_files")
    if not isinstance(allowed, list):
        return []
    return [item for item in allowed if isinstance(item, str) and item.strip()]


def scope_path_matches(path: str, allowed: str) -> bool:
    normalized_path = path.strip().strip('"').strip("/")
    normalized_allowed = allowed.strip().strip('"')
    if normalized_allowed.startswith("./"):
        normalized_allowed = normalized_allowed[2:]
    if not normalized_path or not normalized_allowed:
        return False
    if normalized_allowed.endswith("/"):
        prefix = normalized_allowed.strip("/")
        return normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
    if any(char in normalized_allowed for char in "*?[]"):
        return fnmatchcase(normalized_path, normalized_allowed)
    return normalized_path == normalized_allowed.strip("/")


def format_workspace_status_change(
    before: list[str],
    after: list[str],
    role: str = "planner",
    rule: str = "plan 승인 전에 workspace 파일을 변경하면 안 됩니다",
) -> str:
    title = role.replace("_", " ").title()
    return f"""# {title} Write 위반

{role} role이 규칙을 위반했습니다: {rule}

## 이전 상태
{format_status_lines(before)}

## 이후 상태
{format_status_lines(after)}
"""


def source_repo_snapshot(repo_url: str, data_root: Path) -> dict[str, object]:
    repo_path = Path(repo_url).expanduser()
    if not repo_path.exists():
        return {"enabled": False, "reason": "repo가 local path가 아닙니다"}

    root_result = run_pipeline_git(["git", "rev-parse", "--show-toplevel"], cwd=repo_path)
    if root_result.exit_code != 0:
        return {"enabled": False, "reason": "local path가 git repo가 아닙니다"}

    repo_root = Path(root_result.combined_output.strip()).resolve()
    head = run_pipeline_git(["git", "rev-parse", "HEAD"], cwd=repo_root).combined_output.strip()
    status = run_pipeline_git(
        ["git", "status", "--ignored", "--porcelain", "--untracked-files=all"],
        cwd=repo_root,
    ).combined_output
    excluded = excluded_source_paths(repo_root, data_root)
    return {
        "enabled": True,
        "path": str(repo_root),
        "head": head,
        "status": [line for line in status.splitlines() if not status_line_is_excluded(line, excluded)],
        "excluded_paths": sorted(excluded),
    }


def source_repo_violation(grant: dict[str, object], data_root: Path) -> str | None:
    before = grant.get("source_repo_snapshot")
    if not isinstance(before, dict) or not before.get("enabled"):
        return None

    repo_path = before.get("path")
    if not isinstance(repo_path, str) or not repo_path:
        return "source repo guard 실패: execution grant에 source repo path가 없습니다"

    after = source_repo_snapshot(repo_path, data_root)
    if not after.get("enabled"):
        return f"source repo guard 실패: {after.get('reason', 'source repo를 사용할 수 없습니다')}"

    before_head = before.get("head")
    after_head = after.get("head")
    before_status = before.get("status")
    after_status = after.get("status")
    if before_head == after_head and before_status == after_status:
        return None

    return f"""run workspace 밖 source repo가 변경되었습니다

## 이전 상태
HEAD: {before_head}
Status:
{format_status_lines(before_status)}

## 이후 상태
HEAD: {after_head}
Status:
{format_status_lines(after_status)}
"""


def run_pipeline_git(args: list[str], cwd: Path) -> CommandResult:
    return run_args(args, cwd=cwd, env=sanitized_child_env())


def excluded_source_paths(repo_root: Path, data_root: Path) -> set[str]:
    try:
        relative = data_root.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return set()
    return {relative.as_posix().rstrip("/")}


def status_line_is_excluded(line: str, excluded_paths: set[str]) -> bool:
    if not excluded_paths:
        return False
    paths = status_line_paths(line)
    return bool(paths) and all(path_is_excluded(path, excluded_paths) for path in paths)


def status_line_paths(line: str) -> list[str]:
    if len(line) < 4:
        return []
    path = line[3:].strip()
    if " -> " in path:
        return [status_path(part) for part in path.split(" -> ", 1)]
    return [status_path(path)]


def status_path(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return text[1:-1]
        if isinstance(decoded, str):
            return decoded
    return text


def status_output_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        paths.extend(status_line_paths(line))
    return paths


def path_is_excluded(path: str, excluded_paths: set[str]) -> bool:
    normalized = path.strip("/").strip('"')
    return any(normalized == excluded or normalized.startswith(f"{excluded}/") for excluded in excluded_paths)


def format_status_lines(value: object) -> str:
    if isinstance(value, list) and value:
        return "\n".join(f"- {line}" for line in value)
    if isinstance(value, list):
        return "- clean"
    return f"- {value}"
