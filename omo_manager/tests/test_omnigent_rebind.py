from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_omnigent_rebind import BRIDGE_LABEL, main


OLD = "omnigent://old-session"
NEW = "omnigent://new-session"
MANAGER = "dw:59"
WORKSPACE = "/work/paper"
TITLE = "webconf_autonomy2"
BLOCKER = "OmniGent rotated the native Codex terminal before its first turn completed"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task(*, runat: str = OLD, manager: str = MANAGER, pending: bool = False) -> bytes:
    pending_text = "pending_task_items:\n  - keep me\n" if pending else "pending_task_items: []\n"
    return (
        "---\n"
        "version: v1.0.0\n"
        "status: blocked\n"
        f"blocked_on: {BLOCKER}\n"
        f"runat: {runat}\n"
        "tool: codex\n"
        f"managerat: {manager}\n"
        "is_manager: false\n"
        f"{pending_text}"
        "---\n"
        "<manager_delegation from=\"dw:59\">\n"
        "Advance the paper.\n"
        "</manager_delegation>\n"
    ).encode()


def resource_item(event_type: str, item_id: str) -> dict[str, object]:
    return {
        "id": item_id,
        "type": "resource_event",
        "status": "completed",
        "response_id": item_id,
        "created_at": 10,
        "data": {
            "event_type": event_type,
            "resource_id": "terminal_codex_main",
            "resource_type": "terminal",
        },
    }


class OmniGentRotationRebindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "work_logs"
        self.root.mkdir()
        self.task_path = self.root / "webconf_autonomy2.md"
        self.todo_path = self.root / "TODO.md"
        self.task_path.write_bytes(task())
        self.todo_path.write_text(f"current:\nwebconf_autonomy2.md {OLD}\n\nhuman pending:\n\nprevious:\n", encoding="utf-8")
        body = task().split(b"\n---\n", 1)[1]
        self.prompt = b"$ getagentsmd\ninstructions\n" + body
        self.packet = self.base / "packet.json"
        self.review = self.base / "review.json"
        self.audit = self.base / "audit.json"
        self.old = {
            "id": "old-session",
            "agent_id": "agent-codex",
            "harness": "codex-native",
            "status": "running",
            "runner_id": None,
            "runner_online": True,
            "host_id": "host-1",
            "host_online": True,
            "workspace": WORKSPACE,
            "title": TITLE,
            "external_session_id": "thread-old",
            "created_at": 1,
            "updated_at": 10,
            "labels": {},
            "items": [
                resource_item("session.resource.created", "create-old"),
                {
                    "id": "prompt",
                    "type": "message",
                    "status": "completed",
                    "response_id": "turn-old",
                    "created_at": 10,
                    "data": {"role": "user", "content": [{"type": "input_text", "text": self.prompt.decode()}]},
                },
                resource_item("session.resource.deleted", "delete-old"),
            ],
        }
        self.new = {
            "id": "new-session",
            "agent_id": "agent-codex",
            "harness": "codex-native",
            "status": "idle",
            "runner_id": "runner-1",
            "runner_online": True,
            "host_id": None,
            "host_online": None,
            "workspace": None,
            "title": None,
            "external_session_id": "thread-new",
            "created_at": 10,
            "updated_at": 10,
            "labels": {BRIDGE_LABEL: "old-session"},
            "items": [resource_item("session.resource.created", "create-new")],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, method: str, path: str, _payload: object | None = None) -> object:
        self.assertEqual("GET", method)
        if path.startswith("/v1/sessions/old-session?"):
            return self.old
        if path.startswith("/v1/sessions/new-session?"):
            return self.new
        if path == "/v1/sessions?limit=1000":
            return {
                "has_more": False,
                "data": [
                    {"id": "new-session", "labels": {BRIDGE_LABEL: "old-session"}},
                    {"id": "old-session", "labels": {}},
                ],
            }
        self.fail(f"unexpected request {method} {path}")

    def prepare_args(self) -> list[str]:
        return [
            "--prepare",
            "--root",
            str(self.root),
            "--task-file",
            self.task_path.name,
            "--old-target",
            OLD,
            "--new-target",
            NEW,
            "--expected-task-sha256",
            sha(self.task_path.read_bytes()),
            "--expected-todo-sha256",
            sha(self.todo_path.read_bytes()),
            "--expected-manager-target",
            MANAGER,
            "--expected-workspace",
            WORKSPACE,
            "--expected-title",
            TITLE,
            "--expected-prompt-sha256",
            sha(self.prompt),
            "--output",
            str(self.packet),
            "--audit-output",
            str(self.audit),
        ]

    def prepare_and_review(self) -> tuple[str, str]:
        self.assertEqual(0, main(self.prepare_args()))
        packet_sha = sha(self.packet.read_bytes())
        self.assertEqual(
            0,
            main(
                [
                    "--review",
                    "--packet",
                    str(self.packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(self.review),
                ]
            ),
        )
        return packet_sha, sha(self.review.read_bytes())

    def execute_args(self, packet_sha: str, review_sha: str) -> list[str]:
        return [
            "--execute",
            "--packet",
            str(self.packet),
            "--packet-sha256",
            packet_sha,
            "--review-report",
            str(self.review),
            "--review-report-sha256",
            review_sha,
        ]

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_success_rebinds_only_task_and_todo_and_writes_audit(self, request) -> None:
        request.side_effect = self.request
        packet_sha, review_sha = self.prepare_and_review()
        before_body = self.task_path.read_bytes().split(b"\n---\n", 1)[1]

        self.assertEqual(0, main(self.execute_args(packet_sha, review_sha)))

        self.assertIn(f"runat: {NEW}", self.task_path.read_text())
        self.assertEqual(before_body, self.task_path.read_bytes().split(b"\n---\n", 1)[1])
        self.assertIn(f"webconf_autonomy2.md {NEW}", self.todo_path.read_text())
        self.assertNotIn(OLD, self.todo_path.read_text())
        self.assertEqual("complete", json.loads(self.audit.read_text())["state"])
        self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_manager_authority_mismatch_writes_nothing(self, request) -> None:
        request.side_effect = self.request
        self.task_path.write_bytes(task(manager="dw:58"))
        args = self.prepare_args()
        args[args.index("--expected-task-sha256") + 1] = sha(self.task_path.read_bytes())
        self.assertEqual(2, main(args))
        self.assertFalse(self.packet.exists())
        self.assertIn(OLD, self.task_path.read_text())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_lineage_mismatch_writes_nothing(self, request) -> None:
        self.new["labels"] = {BRIDGE_LABEL: "someone-else"}
        request.side_effect = self.request
        self.assertEqual(2, main(self.prepare_args()))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_queue_drift_writes_nothing(self, request) -> None:
        request.side_effect = self.request
        self.task_path.write_bytes(task(pending=True))
        args = self.prepare_args()
        args[args.index("--expected-task-sha256") + 1] = sha(self.task_path.read_bytes())
        self.assertEqual(2, main(args))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_runner_drift_writes_nothing(self, request) -> None:
        self.new["runner_online"] = False
        request.side_effect = self.request
        self.assertEqual(2, main(self.prepare_args()))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_malformed_old_runner_id_writes_nothing(self, request) -> None:
        self.old["runner_id"] = []
        request.side_effect = self.request
        self.assertEqual(2, main(self.prepare_args()))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_noncompleted_prompt_is_not_treated_as_consumed(self, request) -> None:
        self.old["items"][1]["status"] = "queued"  # type: ignore[index]
        request.side_effect = self.request
        self.assertEqual(2, main(self.prepare_args()))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_crlf_only_prompt_body_difference_is_accepted(self, request) -> None:
        self.prompt = self.prompt.replace(b"Advance the paper.\n", b"Advance the paper.\r\n", 1)
        self.old["items"][1]["data"]["content"][0]["text"] = self.prompt.decode()  # type: ignore[index]
        request.side_effect = self.request
        self.assertEqual(0, main(self.prepare_args()))
        self.assertTrue(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_prompt_body_content_drift_is_rejected(self, request) -> None:
        self.prompt = self.prompt.replace(b"Advance the paper.", b"Advance a different paper.", 1)
        self.old["items"][1]["data"]["content"][0]["text"] = self.prompt.decode()  # type: ignore[index]
        request.side_effect = self.request
        self.assertEqual(2, main(self.prepare_args()))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_wrong_resource_type_does_not_prove_terminal_transfer(self, request) -> None:
        self.new["items"][0]["data"]["resource_type"] = "file"  # type: ignore[index]
        request.side_effect = self.request
        self.assertEqual(2, main(self.prepare_args()))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_blank_external_thread_id_does_not_prove_rotation(self, request) -> None:
        self.new["external_session_id"] = ""
        request.side_effect = self.request
        self.assertEqual(2, main(self.prepare_args()))
        self.assertFalse(self.packet.exists())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_task_concurrency_after_review_fails_before_both_writes(self, request) -> None:
        request.side_effect = self.request
        packet_sha, review_sha = self.prepare_and_review()
        original_todo = self.todo_path.read_bytes()
        self.task_path.write_bytes(self.task_path.read_bytes() + b"concurrent\n")
        changed_task = self.task_path.read_bytes()

        self.assertEqual(2, main(self.execute_args(packet_sha, review_sha)))

        self.assertEqual(changed_task, self.task_path.read_bytes())
        self.assertEqual(original_todo, self.todo_path.read_bytes())
        self.assertEqual("prepared", json.loads(self.audit.read_text())["state"])

    @patch("omo_manager.omo_omnigent_rebind.replace_if_unchanged_locked")
    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_second_write_failure_rolls_back_todo(self, request, replace) -> None:
        request.side_effect = self.request
        packet_sha, review_sha = self.prepare_and_review()
        original_task = self.task_path.read_bytes()
        original_todo = self.todo_path.read_bytes()
        from omo_manager.omo_task_status import replace_if_unchanged_locked as real_replace

        calls = 0

        def fail_second(path: Path, text: str, state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected task write failure")
            return real_replace(path, text, state)

        replace.side_effect = fail_second
        self.assertEqual(2, main(self.execute_args(packet_sha, review_sha)))
        self.assertEqual(original_task, self.task_path.read_bytes())
        self.assertEqual(original_todo, self.todo_path.read_bytes())
        self.assertEqual("prepared", json.loads(self.audit.read_text())["state"])

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_reviewed_partial_todo_state_is_recovered_without_duplicate_session(self, request) -> None:
        request.side_effect = self.request
        packet_sha, review_sha = self.prepare_and_review()
        packet = json.loads(self.packet.read_text())
        self.todo_path.write_bytes(__import__("base64").b64decode(packet["todo_after"], validate=True))

        self.assertEqual(0, main(self.execute_args(packet_sha, review_sha)))

        self.assertIn(f"runat: {NEW}", self.task_path.read_text())
        self.assertIn(f"webconf_autonomy2.md {NEW}", self.todo_path.read_text())

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_impossible_task_first_partial_state_is_rejected(self, request) -> None:
        request.side_effect = self.request
        packet_sha, review_sha = self.prepare_and_review()
        packet = json.loads(self.packet.read_text())
        import base64

        self.task_path.write_bytes(base64.b64decode(packet["task_after"], validate=True))
        original_todo = self.todo_path.read_bytes()

        self.assertEqual(2, main(self.execute_args(packet_sha, review_sha)))

        self.assertIn(f"runat: {NEW}", self.task_path.read_text())
        self.assertEqual(original_todo, self.todo_path.read_bytes())
        self.assertEqual("prepared", json.loads(self.audit.read_text())["state"])

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_competing_owner_blocks_partial_recovery(self, request) -> None:
        request.side_effect = self.request
        packet_sha, review_sha = self.prepare_and_review()
        packet = json.loads(self.packet.read_text())
        import base64

        self.todo_path.write_bytes(base64.b64decode(packet["todo_after"], validate=True))
        competitor = self.root / "competitor.md"
        competitor.write_bytes(task(runat=NEW))
        original_task = self.task_path.read_bytes()

        self.assertEqual(2, main(self.execute_args(packet_sha, review_sha)))

        self.assertEqual(original_task, self.task_path.read_bytes())
        self.assertIn(f"webconf_autonomy2.md {NEW}", self.todo_path.read_text())
        self.assertEqual("prepared", json.loads(self.audit.read_text())["state"])

    @patch("omo_manager.omo_omnigent_rebind.request_json")
    def test_conflicting_final_audit_is_rejected_before_lifecycle_writes(self, request) -> None:
        request.side_effect = self.request
        packet_sha, review_sha = self.prepare_and_review()
        original_task = self.task_path.read_bytes()
        original_todo = self.todo_path.read_bytes()
        self.audit.write_text("conflict\n", encoding="utf-8")
        self.audit.chmod(0o600)

        self.assertEqual(2, main(self.execute_args(packet_sha, review_sha)))

        self.assertEqual(original_task, self.task_path.read_bytes())
        self.assertEqual(original_todo, self.todo_path.read_bytes())
        self.assertEqual("conflict\n", self.audit.read_text())


if __name__ == "__main__":
    unittest.main()
