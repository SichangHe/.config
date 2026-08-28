from __future__ import annotations

import unittest
from pathlib import Path

from omo_manager import omo_pending_watch as watcher


class ProblemOwnerRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = watcher.Args(
            Path("/tmp"),
            "",
            Path("/tmp/seen.tsv"),
            1.0,
            1.0,
            30.0,
            Path("/status.py"),
            False,
            True,
            manager_target="agent_managers:0",
        )

    def test_stale_notice_owner_text_does_not_override_fresh_unowned_route(self) -> None:
        line = (
            "untracked_agent: task=tmux:dw:2 evidence=target=dw:2 role=tmux_unmanaged "
            "output=old notice owner_target=amh:1 route_owner_target=-"
        )

        groups = watcher.agent_problem_output_by_owner(
            self.args,
            {},
            f"agent-problems: untracked_agent=1\n{line}\n",
            1000.0,
        )

        self.assertEqual([""], list(groups))
        row = watcher.parse_problem_row(line)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("", row.owner_target)

    def test_structured_owner_routes_ready_worker_to_manager_not_worker(self) -> None:
        line = (
            "ready: task=worker.md evidence=target=dw2:0 task_status=running output=idle "
            "owner_target=agent_managers:0 route_owner_target=agent_managers:0"
        )

        groups = watcher.agent_problem_output_by_owner(
            self.args,
            {},
            f"agent-problems: ready=1\n{line}\n",
            1000.0,
        )

        self.assertEqual(["agent_managers:0"], list(groups))
        self.assertIn(
            "worker.md dw2:0 <output>idle</output>",
            groups["agent_managers:0"].text,
        )


if __name__ == "__main__":
    unittest.main()
