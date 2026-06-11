from __future__ import annotations

from devauto.core.models import RunStatus


ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.RECEIVED: {RunStatus.QUEUED, RunStatus.PREPARING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.QUEUED: {RunStatus.PREPARING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.PREPARING: {
        RunStatus.PLAN_REVIEWED,
        RunStatus.AWAITING_PLAN_APPROVAL,
        RunStatus.PROVISIONING,
        RunStatus.ESCALATED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.PLAN_REVIEWED: {
        RunStatus.AWAITING_PLAN_APPROVAL,
        RunStatus.PROVISIONING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.AWAITING_PLAN_APPROVAL: {
        RunStatus.PREPARING,
        RunStatus.PROVISIONING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.PROVISIONING: {RunStatus.EXECUTING, RunStatus.ESCALATED, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.EXECUTING: {
        RunStatus.DETERMINISTIC_CHECKS,
        RunStatus.FIXING,
        RunStatus.ESCALATED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.FIXING: {
        RunStatus.EXECUTING,
        RunStatus.DETERMINISTIC_CHECKS,
        RunStatus.ESCALATED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.DETERMINISTIC_CHECKS: {
        RunStatus.AI_REVIEWING,
        RunStatus.FIXING,
        RunStatus.AWAITING_QA_APPROVAL,
        RunStatus.READY_TO_PUBLISH,
        RunStatus.AWAITING_PUBLISH_APPROVAL,
        RunStatus.PUBLISHED,
        RunStatus.ESCALATED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.AI_REVIEWING: {
        RunStatus.FIXING,
        RunStatus.AWAITING_QA_APPROVAL,
        RunStatus.READY_TO_PUBLISH,
        RunStatus.AWAITING_PUBLISH_APPROVAL,
        RunStatus.PUBLISHED,
        RunStatus.ESCALATED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.AWAITING_QA_APPROVAL: {
        RunStatus.READY_TO_PUBLISH,
        RunStatus.AWAITING_PUBLISH_APPROVAL,
        RunStatus.ESCALATED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.READY_TO_PUBLISH: {
        RunStatus.AWAITING_PUBLISH_APPROVAL,
        RunStatus.PUBLISHING,
        RunStatus.PUBLISHED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.AWAITING_PUBLISH_APPROVAL: {
        RunStatus.PUBLISHING,
        RunStatus.ESCALATED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.PUBLISHING: {RunStatus.PUBLISHED, RunStatus.ESCALATED, RunStatus.FAILED},
    RunStatus.PUBLISHED: set(),
    RunStatus.ESCALATED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def can_cancel(status: RunStatus) -> bool:
    return can_transition(status, RunStatus.CANCELLED)


def assert_transition(current: RunStatus, target: RunStatus) -> None:
    if not can_transition(current, target):
        raise ValueError(f"잘못된 run status 전이입니다: {current} -> {target}")
