import unittest

from dev_agent.domain.models import TaskStatus
from dev_agent.domain.state_machine import ensure_transition


class StateMachineTests(unittest.TestCase):
    def test_allows_expected_transition(self):
        ensure_transition(TaskStatus.REQUIREMENT_ANALYSIS, TaskStatus.WAITING_REQUIREMENT_APPROVAL)

    def test_rejects_skipping_human_gate(self):
        with self.assertRaisesRegex(ValueError, "Illegal task transition"):
            ensure_transition(TaskStatus.WAITING_REQUIREMENT_APPROVAL, TaskStatus.CHANGE_READY)

    def test_release_gate_can_return_to_changes_requested(self):
        ensure_transition(
            TaskStatus.WAITING_RELEASE_APPROVAL,
            TaskStatus.CHANGES_REQUESTED,
        )

    def test_open_pull_request_can_return_to_changes_requested(self):
        ensure_transition(
            TaskStatus.WAITING_MERGE_APPROVAL,
            TaskStatus.CHANGES_REQUESTED,
        )
