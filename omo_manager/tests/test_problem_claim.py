from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_pending_watch as watcher
from omo_manager.amh_problem_claim import DEFAULT_CLAIM_LEASE_S, claim_problem, issue_path, issue_problem, legacy_issue_path, prune_resolved_claims, read_claims, read_issues, read_state, sync_problem_issues


class ProblemClaimTest(unittest.TestCase):
    def test_claim_has_fixed_ten_minute_lease_and_concrete_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            claim = claim_problem(path, "0123456789abcdef", "wl:1", "  inspect   the pane  ", 100.0)
            self.assertEqual("inspect the pane", claim.action)
            self.assertEqual(100.0 + DEFAULT_CLAIM_LEASE_S, claim.expires_at_s)
            self.assertEqual(claim, read_claims(path)[claim.problem_id])

    def test_claim_rejects_empty_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "concrete next action"):
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            claim_problem(path, "0123456789abcdef", "wl:1", " \n ", 100.0)

    def test_claim_rejects_multiple_command_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            with self.assertRaisesRegex(ValueError, "one line without command separators"):
                claim_problem(path, "0123456789abcdef", "wl:1", "inspect; then restart", 100.0)

    def test_unchanged_problem_cannot_reset_its_claim_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            claim_problem(path, "0123456789abcdef", "wl:1", "inspect", 100.0)
            with self.assertRaisesRegex(ValueError, "already claimed"):
                claim_problem(path, "0123456789abcdef", "wl:1", "try again", 800.0)

    def test_only_matching_unexpired_manager_claim_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            claim = claim_problem(path, "0123456789abcdef", "wl:1.0", "inspect", 100.0)
            claims = read_claims(path)
            self.assertEqual(claim, watcher.active_problem_claim(claims, claim.problem_id, "wl:1", 699.0))
            self.assertIsNone(watcher.active_problem_claim(claims, claim.problem_id, "wl:2", 699.0))
            self.assertIsNone(watcher.active_problem_claim(claims, claim.problem_id, "wl:1", 700.0))

    def test_independent_scan_prunes_resolved_problem_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            claim_problem(path, "0123456789abcdef", "wl:1", "inspect", 100.0)
            prune_resolved_claims(path, set())
            self.assertEqual({}, read_claims(path))

    def test_expired_claim_reopens_and_active_claim_suppresses_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = watcher.Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, root / "status.py", False, False, manager_target="wl:1", agent_problem_repeat_s=1800.0)
            line = "error: task=worker.md evidence=target=vl:2 output=boom owner_target=wl:1"
            output = f"agent-problems: error=1\n{line}\n"
            dispatch = watcher.agent_problem_output_by_owner(args, {}, output, 100.0)["wl:1"]
            problem_id = watcher.problem_claim_id("wl:1", dispatch.problem_lines)
            sync_problem_issues(watcher.problem_claim_path(args), {problem_id: ("wl:1", dispatch.problem_lines)}, 90.0)
            claim_problem(watcher.problem_claim_path(args), problem_id, "wl:1", "inspect the pane", 100.0)
            result = watcher.CommandOutput("agent-problems", 3, output, "")
            common = (
                patch.object(watcher, "push_agent_pending_item_reminders", return_value=False),
                patch.object(watcher, "push_manager_direct_report_reminders", return_value=False),
                patch.object(watcher, "maybe_push_dependency_transitions", return_value=False),
                patch.object(watcher, "maybe_push_manager_compaction_reminder", return_value=False),
                patch.object(watcher, "route_or_email_manager_problem", return_value=False),
                patch.object(watcher, "agent_problem_target_is_ready", return_value=True),
            )
            with common[0], common[1], common[2], common[3], common[4], common[5], patch.object(watcher, "push_manager_text_to_target") as push:
                self.assertFalse(watcher.handle_agent_problem_result(args, {}, result, 200.0))
                push.assert_not_called()
            with common[0], common[1], common[2], common[3], common[4], common[5], patch.object(watcher, "push_manager_text_to_target", return_value=0) as push:
                self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 701.0))
                self.assertIn("Previously claimed by wl:1 but still present after 10 minutes", push.call_args.args[1])
                self.assertIn(f"amh_problem.py claim {problem_id}", push.call_args.args[1])
            self.assertIn(problem_id, read_claims(watcher.problem_claim_path(args)))

    def test_pre_paste_guard_rejects_claim_created_after_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "claims.json"
            problem_id = "0123456789abcdef"
            sync_problem_issues(path, {problem_id: ("wl:1", ("error: row",))}, 90.0)
            claim_problem(path, problem_id, "wl:1", "inspect", 100.0)
            guard = watcher.AgentProblemGuard(("status",), ("error: row",), problem_id=problem_id, problem_owner_target="wl:1", problem_claim_path=path)
            self.assertFalse(watcher.agent_problem_guard_current(guard))

    def test_non_recipient_cannot_claim_or_poison_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            with self.assertRaisesRegex(ValueError, "issued to wl:1"):
                claim_problem(path, "0123456789abcdef", "wl:2", "inspect", 100.0)
            self.assertEqual({}, read_claims(path))

    def test_clean_issue_sync_prunes_issue_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            sync_problem_issues(path, {"0123456789abcdef": ("wl:1", ("error: row",))}, 90.0)
            claim_problem(path, "0123456789abcdef", "wl:1", "inspect", 100.0)
            sync_problem_issues(path, {}, 200.0)
            self.assertEqual({}, read_claims(path))
            self.assertEqual({}, read_issues(issue_path(path)))

    def test_problem_id_uses_untruncated_raw_evidence(self) -> None:
        prefix = "x" * 2500
        first = watcher.problem_claim_id("wl:1", (f"error: {prefix} first",))
        second = watcher.problem_claim_id("wl:1", (f"error: {prefix} second",))
        self.assertNotEqual(first, second)

    def test_issued_subset_survives_additional_problem_but_not_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claims.json"
            first_line = "error: first"
            second_line = "error: second"
            first_id = watcher.problem_claim_id("wl:1", (first_line,))
            issue_problem(path, first_id, "wl:1", (first_line,), 100.0)
            combined_id = watcher.problem_claim_id("wl:1", (first_line, second_line))
            sync_problem_issues(path, {combined_id: ("wl:1", (first_line, second_line))}, 110.0)
            self.assertIn(first_id, read_issues(issue_path(path)))
            sync_problem_issues(path, {watcher.problem_claim_id("wl:1", (second_line,)): ("wl:1", (second_line,))}, 120.0)
            self.assertNotIn(first_id, read_issues(issue_path(path)))

    def test_authoritative_groups_do_not_apply_unstick_attempt_suppression(self) -> None:
        root = Path("/tmp")
        args = watcher.Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, root / "status.py", False, False, manager_target="wl:1")
        line = "stuck_input: task=worker.md evidence=target=vl:2 input=queued owner_target=wl:1 unstick=sent_enter"
        self.assertEqual({"wl:1": (line,)}, watcher.authoritative_problem_groups(args, f"agent-problems: stuck_input=1\n{line}\n"))

    def test_legacy_flat_state_and_issue_sidecar_migrate_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            problem_id = "0123456789abcdef"
            path.write_text(json.dumps({problem_id: {"problem_id": problem_id, "manager_target": "wl:1", "action": "inspect", "claimed_at_s": 100.0, "expires_at_s": 700.0}}), encoding="utf-8")
            legacy_issue_path(path).write_text(json.dumps({problem_id: {"problem_id": problem_id, "manager_target": "wl:1", "issued_at_s": 90.0, "problem_lines": ["error: row"]}}), encoding="utf-8")
            state = read_state(path)
            self.assertIn(problem_id, state.claims)
            self.assertIn(problem_id, state.issues)
            sync_problem_issues(path, {problem_id: ("wl:1", ("error: row",))}, 110.0)
            self.assertFalse(legacy_issue_path(path).exists())
            self.assertEqual(1, json.loads(path.read_text(encoding="utf-8"))["version"])

    def test_unknown_state_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            path.write_text('{"version": 2, "issues": {}, "claims": {}}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported AMH problem-state version"):
                read_state(path)

    def test_corrupt_state_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot inspect AMH problem state"):
                sync_problem_issues(path, {}, 100.0)
            self.assertEqual("{broken", path.read_text(encoding="utf-8"))

    def test_corrupt_legacy_issue_state_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            path.write_text("{}", encoding="utf-8")
            legacy_issue_path(path).write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "legacy AMH problem state"):
                sync_problem_issues(path, {}, 100.0)
            self.assertEqual("{}", path.read_text(encoding="utf-8"))
            self.assertEqual("{broken", legacy_issue_path(path).read_text(encoding="utf-8"))

    def test_sidecar_only_legacy_state_migrates_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            problem_id = "0123456789abcdef"
            legacy_issue_path(path).write_text(json.dumps({problem_id: {"problem_id": problem_id, "manager_target": "wl:1", "issued_at_s": 90.0, "problem_lines": ["error: row"]}}), encoding="utf-8")
            self.assertIn(problem_id, read_state(path).issues)
            sync_problem_issues(path, {problem_id: ("wl:1", ("error: row",))}, 100.0)
            self.assertIn(problem_id, read_state(path).issues)
            self.assertFalse(legacy_issue_path(path).exists())

    def test_schema_invalid_state_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            raw = '{"version": 1, "issues": {}, "claims": {"0123456789abcdef": {"problem_id": "different"}}}\n'
            path.write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid AMH problem claim"):
                sync_problem_issues(path, {}, 100.0)
            self.assertEqual(raw, path.read_text(encoding="utf-8"))

    def test_persisted_claim_cannot_bypass_action_or_lease_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            problem_id = "0123456789abcdef"
            claim = {"problem_id": problem_id, "manager_target": "wl:1", "action": "inspect; dismiss", "claimed_at_s": 100.0, "expires_at_s": 10000.0}
            path.write_text(json.dumps({"version": 1, "issues": {}, "claims": {problem_id: claim}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid AMH problem claim"):
                read_state(path)

    def test_persisted_claim_action_must_be_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "amh-problem-claims.json"
            problem_id = "0123456789abcdef"
            claim = {"problem_id": problem_id, "manager_target": "wl:1", "action": "inspect   pane", "claimed_at_s": 100.0, "expires_at_s": 700.0}
            path.write_text(json.dumps({"version": 1, "issues": {}, "claims": {problem_id: claim}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid AMH problem claim"):
                read_state(path)

    def test_path_entry_point_runs_outside_repository(self) -> None:
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "PATH": f"{root / 'bin'}:{os.environ.get('PATH', '')}"}
        result = subprocess.run(["amh_problem.py", "--help"], cwd="/tmp", env=env, capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
