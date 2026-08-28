from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from omo_manager.omo_agent_audit import AuditConfig, compact_jsonl_tail, sample_eligible_agents, review_audit, temporary_audit_workspace, cleanup_stale_audit_dirs


class AgentAuditTests(unittest.TestCase):
    def test_envelope_filtering_bounds_and_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            rows = [
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "progress update"}},
                {"type": "session_meta", "payload": {"id": "good"}},
                {"type": "response_item", "payload": {"type": "message", "id": "item-not-session", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}},
                {"type": "response_item", "session_id": "bad", "payload": {"type": "message", "role": "assistant", "content": "wrong"}},
                {"type": "response_item", "session_id": "good", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
                {"type": "response_item", "session_id": "good", "payload": {"type": "function_call_output", "call_id": "c1", "output": "secret output"}},
                {"type": "response_item", "session_id": "good", "payload": {"type": "reasoning", "summary": "private"}},
                {"type": "future_event"},
                "bad",
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows))
            result = compact_jsonl_tail(path, AuditConfig(enabled=True, message_chars=10, tool_chars=5), session_id="good", tool_output_dir=Path(tmp) / "out")
            self.assertEqual("progress …", result.messages[0]["text"])
            self.assertEqual("answer", result.messages[1]["text"])
            self.assertEqual("c1", result.tool_outputs[0]["call_id"])
            self.assertTrue(result.tool_outputs[0]["path"].endswith("c1.txt"))
            self.assertNotIn("secret", json.dumps(result.tool_outputs))
            self.assertEqual("c1", result.tool_calls[0]["call_id"])
            self.assertEqual(2, result.unknown_records)
            self.assertEqual((), compact_jsonl_tail(path).messages)

    def test_random_durable_cooldown_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            selected = sample_eligible_agents(["a", "b", "c"], state, now=10, cooldown=5, sample_size=2)
            self.assertEqual(2, len(selected))
            self.assertEqual((), sample_eligible_agents(selected, state, now=11, cooldown=5, sample_size=2))
            old = root / "omo-audit-old"
            old.mkdir()
            old.touch()
            import os

            os.utime(old, (1, 1))
            self.assertEqual(1, cleanup_stale_audit_dirs(root, now=100, older_than=10))

    def test_review_and_workspace_cleanup(self) -> None:
        verdict = review_audit(compact_jsonl_tail(Path("/missing"), AuditConfig(enabled=True)))
        self.assertEqual(("inconclusive", True), (verdict.verdict, verdict.escalate))
        with temporary_audit_workspace() as path:
            marker = path / "x"
            marker.write_text("x")
            self.assertTrue(marker.exists())
        self.assertFalse(path.exists())

    def test_review_detects_loop_and_tool_files_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "s.jsonl"
            rows = [{"type": "response_item", "session_id": "s", "payload": {"type": "message", "role": "assistant", "content": "same"}}] * 3
            rows.append({"type": "response_item", "session_id": "s", "payload": {"type": "function_call_output", "call_id": "c", "output": "out"}})
            path.write_text("\n".join(json.dumps(row) for row in rows))
            output_dir = root / "outputs"
            tail = compact_jsonl_tail(path, AuditConfig(enabled=True), session_id="s", tool_output_dir=output_dir)
            verdict = review_audit(tail)
            self.assertEqual("fail", verdict.verdict)
            self.assertEqual(0o700, output_dir.stat().st_mode & 0o777)
            self.assertEqual(0o600, next(output_dir.iterdir()).stat().st_mode & 0o777)

    def test_confirmed_problem_uses_exact_process_guarded_manager_sender(self) -> None:
        from omo_manager.omo_agent_audit import ReviewVerdict, deliver_manager_escalation

        sent: list[str] = []
        with patch("omo_manager.omo_agent_audit.manager_task_path", return_value=Path("/tmp/manager.md")), patch("omo_manager.omo_codex_start.resolve_pane", return_value=Mock()) as resolve, patch("omo_manager.omo_codex_start.send_prompt", side_effect=lambda _pane, path: sent.append(path.read_text())) as sender:
            self.assertTrue(deliver_manager_escalation(Path("/tmp"), "mgr:2", "task.md", ReviewVerdict("strong", "problem", "loop")))
        resolve.assert_called_once_with("mgr:2")
        sender.assert_called_once()
        sent_path = sender.call_args.args[1]
        self.assertFalse(sent_path.exists())
        self.assertIn('<agent_message from="audit:0">', sent[0])


if __name__ == "__main__":
    unittest.main()
