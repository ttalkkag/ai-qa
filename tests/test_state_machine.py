from __future__ import annotations

import unittest

from devauto.core.models import RunStatus
from devauto.core.state_machine import can_cancel, can_transition


class StateMachineTest(unittest.TestCase):
    def test_cancel_is_allowed_for_waiting_and_execution_boundary_states(self) -> None:
        for status in [
            RunStatus.RECEIVED,
            RunStatus.QUEUED,
            RunStatus.PREPARING,
            RunStatus.AWAITING_PLAN_APPROVAL,
            RunStatus.PROVISIONING,
            RunStatus.EXECUTING,
            RunStatus.FIXING,
            RunStatus.DETERMINISTIC_CHECKS,
            RunStatus.AI_REVIEWING,
            RunStatus.AWAITING_QA_APPROVAL,
            RunStatus.READY_TO_PUBLISH,
            RunStatus.AWAITING_PUBLISH_APPROVAL,
        ]:
            self.assertTrue(can_cancel(status), status)

    def test_cancel_is_not_allowed_after_terminal_or_publishing_state(self) -> None:
        for status in [RunStatus.PUBLISHING, RunStatus.PUBLISHED, RunStatus.ESCALATED, RunStatus.FAILED, RunStatus.CANCELLED]:
            self.assertFalse(can_cancel(status), status)

    def test_publish_approval_can_be_rejected_to_escalation(self) -> None:
        self.assertTrue(can_transition(RunStatus.AWAITING_PUBLISH_APPROVAL, RunStatus.ESCALATED))

    def test_unhandled_failures_can_end_any_nonterminal_state(self) -> None:
        for status in [
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
            RunStatus.PUBLISHING,
        ]:
            self.assertTrue(can_transition(status, RunStatus.FAILED), status)


if __name__ == "__main__":
    unittest.main()
