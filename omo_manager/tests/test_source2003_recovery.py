from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_completion_email
from omo_manager import omo_pending
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_completion_email import build_completion_email
from omo_manager.omo_completion_email import claim_completion_email
from omo_manager.omo_completion_email import digest_fields
from omo_manager.omo_completion_email import ordinary_pending_purpose
from omo_manager.omo_task_metadata import parse_task_metadata


class Source2003RecoveryTest(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        state: Path,
    ) -> tuple[Path, str, dict[str, object], tuple[str, ...], str]:
        claim_text = (
            "---\n"
            "version: v1.0.0\n"
            "status: blocked\n"
            f"blocked_on: {omo_completion_email.SOURCE2003_BLOCKED_ON}\n"
            "runat: dw:15\n"
            "tool: codex\n"
            "managerat: dw:60\n"
            "is_manager: false\n"
            "pending_task_items: []\n"
            "---\n"
            "existing Pangram work\n"
        )
        task = root / omo_completion_email.SOURCE2003_TASK
        task.write_text(claim_text, encoding="utf-8")

        def unused_binding(item: str) -> tuple[str, str, str, str, str]:
            semantic_key = omo_pending.pending_add_key(root, task, claim_text, (item,))
            plan = build_completion_email(
                root,
                task,
                claim_text,
                "pending item created",
                items=(item,),
                semantic_key=semantic_key,
            )
            assert plan is not None
            self.assertTrue(claim_completion_email(plan))
            authorization = state / "completion-email-authorizations" / plan.key
            return (
                plan.key,
                plan.task_sha256,
                plan.manager_target,
                plan.notice_semantic_key,
                hashlib.sha256(authorization.read_bytes()).hexdigest(),
            )

        claims = tuple(unused_binding(item) for item in omo_completion_email.SOURCE2003_ITEMS)
        unrelated = unused_binding("🧑 unrelated Human request")
        current = claim_text + f"{omo_completion_email.SOURCE2003_SOURCE_POINTER}\n\n" + "(pending)\n" + "(record and delegate manager_mail/85c5dff58359-2004.txt)\n"
        task.write_text(current, encoding="utf-8")

        draft = (
            "Human Source-2003 added three open requests and I acknowledged them as Message-ID "
            f"`{omo_completion_email.SOURCE2003_ACK_MESSAGE_ID}`.\n\n"
            "The supported add path failed before task mutation: `verified email thread lookup failed for route primary: "
            "recent thread lookup exceeded 30s`. `omo_pending.py list` remains empty. Please recover the exact items; "
            "do not send another Human email or weaken Human provenance.\n\n" + "".join(f"{index}. `{item}`\n" for index, item in enumerate(omo_completion_email.SOURCE2003_ITEM_TEXTS, 1))
        )
        draft_dir = root.parent / "drafts"
        draft_dir.mkdir(mode=0o700)
        draft_path = draft_dir / "source2003.md"
        draft_path.write_text(draft, encoding="utf-8")
        draft_path.chmod(0o600)
        draft_sha256 = hashlib.sha256(draft.encode()).hexdigest()
        replay_id = "a" * 64
        receipt_dir = state / "report-receipts"
        receipt_dir.mkdir(mode=0o700)
        commitment_path = receipt_dir / f"{replay_id}.commitment"
        source_task = str(task.resolve())
        manager_task = str((root / omo_completion_email.SOURCE2003_REPORT_MANAGER_TASK).resolve())
        transfer = {
            "authority": {
                "kind": "agent-originated",
                "producer_target": "dw:15",
                "source_task": source_task,
            },
            "commitment_path": str(commitment_path),
            "queue_item": {
                "input_sha256": draft_sha256,
                "manager": manager_task,
                "pointer": "(from agent dw:15 source2003.md)",
                "producer": source_task,
                "replay_id": replay_id,
            },
            "receiver": manager_task,
            "routing": {
                "manager": manager_task,
                "producer_target": "dw:15",
                "requested_manager_target": "dw:60",
                "resolved_manager_target": "dw:60",
                "route_kind": "active-manager-task",
                "task": source_task,
            },
            "schema": "omo-report-transfer-receipt/v1",
        }
        unsigned: dict[str, object] = {
            "allocation": {},
            "commitment": {},
            "preflight": {
                "allocation": {
                    "file": str(draft_path),
                    "file_sha256": draft_sha256,
                    "file_size_bytes": len(draft.encode()),
                },
                "routing_sources": [
                    {
                        "exists": True,
                        "path": source_task,
                        "sha256": hashlib.sha256(claim_text.encode()).hexdigest(),
                        "size_bytes": len(claim_text.encode()),
                    }
                ],
            },
            "replay_id": replay_id,
            "schema": "omo-report-transaction-commitment/v2",
            "transfer": transfer,
        }
        commitment_id = hashlib.sha256(json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        commitment = {**unsigned, "commitment_id": commitment_id}
        commitment_payload = (
            json.dumps(
                commitment,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        commitment_path.write_text(commitment_payload, encoding="utf-8")
        commitment_path.chmod(0o600)

        purpose = ordinary_pending_purpose("pending item created", omo_completion_email.SOURCE2003_ITEMS, "")
        plan = build_completion_email(
            root,
            task,
            current,
            "pending item created",
            items=omo_completion_email.SOURCE2003_ITEMS,
            semantic_key=purpose,
        )
        assert plan is not None
        constants: dict[str, object] = {
            "SOURCE2003_ROOT": str(root.resolve()),
            "SOURCE2003_TASK_SHA256": hashlib.sha256(current.encode()).hexdigest(),
            "SOURCE2003_PRIOR_TASK_SHA256": hashlib.sha256(claim_text.encode()).hexdigest(),
            "SOURCE2003_PRIOR_TASK_SIZE_BYTES": len(claim_text.encode()),
            "SOURCE2003_QUEUE_SHA256": digest_fields("pending-queue-v1"),
            "SOURCE2003_AFTER_QUEUE_SHA256": digest_fields(
                "pending-queue-v1",
                *omo_completion_email.SOURCE2003_ITEMS,
            ),
            "SOURCE2003_ITEMS_SHA256": hashlib.sha256("\0".join(omo_completion_email.SOURCE2003_ITEMS).encode()).hexdigest(),
            "SOURCE2003_PURPOSE_SHA256": purpose,
            "SOURCE2003_PLAN_KEY": plan.key,
            "SOURCE2003_NOTICE_KEY": plan.notice_key,
            "SOURCE2003_CANONICAL_BODY_SHA256": hashlib.sha256(plan.body.encode()).hexdigest(),
            "SOURCE2003_FAILED_CLAIMS": claims,
            "SOURCE2003_REPORT_REPLAY_ID": replay_id,
            "SOURCE2003_REPORT_COMMITMENT_ID": commitment_id,
            "SOURCE2003_REPORT_COMMITMENT_PATH": commitment_path,
            "SOURCE2003_REPORT_COMMITMENT_SHA256": hashlib.sha256(commitment_payload.encode()).hexdigest(),
            "SOURCE2003_REPORT_DRAFT_PATH": draft_path,
            "SOURCE2003_REPORT_DRAFT_SHA256": draft_sha256,
            "SOURCE2003_REPORT_DRAFT_SIZE_BYTES": len(draft.encode()),
        }
        return task, current, constants, tuple(binding[0] for binding in claims), unrelated[0]

    def test_records_exact_items_replays_and_never_sends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            state = Path(tmp) / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, current, constants, claims, unrelated = self.fixture(root, state)
                ledger = state / "completion-email-claims.tsv"
                unrelated_row = next(line for line in ledger.read_text(encoding="utf-8").splitlines() if line.startswith(unrelated + "\t"))
                unrelated_authorization = (state / "completion-email-authorizations" / unrelated).read_bytes()
                with (
                    patch.multiple(omo_completion_email, **constants),  # pyright: ignore[reportCallIssue, reportArgumentType]
                    patch("omo_manager.omo_pending.current_pending_task", return_value=task),
                    patch("omo_manager.omo_completion_email.current_pending_task", return_value=task),
                    patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True) as sent,
                    patch("omo_manager.omo_pending.require_owner_completion") as owner_sender,
                    patch("omo_manager.omo_completion_email.send_completion_email") as sender,
                    patch("omo_manager.omo_completion_email.subprocess.run") as email_process,
                    redirect_stdout(StringIO()),
                ):
                    args = omo_pending.parse_args(["recover-source2003-pangram"])
                    self.assertEqual(omo_completion_email.SOURCE2003_ITEMS, args.items)
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    updated = task.read_text(encoding="utf-8")
                    metadata = parse_task_metadata(updated, root)
                    assert metadata is not None
                    self.assertEqual("blocked", metadata.status)
                    self.assertEqual(omo_completion_email.SOURCE2003_BLOCKED_ON, metadata.blocked_on)
                    self.assertEqual("dw:15", metadata.runat)
                    self.assertEqual("dw:60", metadata.managerat)
                    self.assertEqual(omo_completion_email.SOURCE2003_ITEMS, metadata.pending_task_items)
                    self.assertEqual(current.split("---\n", 2)[2], updated.split("---\n", 2)[2])
                    self.assertEqual(1, updated.splitlines().count(omo_completion_email.SOURCE2003_SOURCE_POINTER))
                    rows = ledger.read_text(encoding="utf-8").splitlines()
                    for item, claim in zip(omo_completion_email.SOURCE2003_ITEMS, claims, strict=True):
                        self.assertEqual(1, metadata.pending_task_items.count(item))
                        self.assertIn("\tretired:", next(line for line in rows if line.startswith(claim + "\t")))
                        self.assertTrue((state / "completion-email-retired-authorizations" / claim).is_file())
                    self.assertIn(unrelated_row, rows)
                    self.assertEqual(
                        unrelated_authorization,
                        (state / "completion-email-authorizations" / unrelated).read_bytes(),
                    )
                    transitions = list((state / "ordinary-pending-transitions").iterdir())
                    self.assertEqual(1, len(transitions))
                    self.assertIn('"status":"committed"', transitions[0].read_text(encoding="utf-8"))
                    snapshot = {path.relative_to(root.parent): path.read_bytes() for path in root.parent.rglob("*") if path.is_file()}
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    self.assertEqual(
                        snapshot,
                        {path.relative_to(root.parent): path.read_bytes() for path in root.parent.rglob("*") if path.is_file()},
                    )
                sent.assert_called_once_with(
                    omo_completion_email.SOURCE2003_ACK_MESSAGE_ID,
                    omo_completion_email.SOURCE2003_ACK_SUBJECT_SHA256,
                    omo_completion_email.SOURCE2003_ACK_BODY_SHA256,
                )
                owner_sender.assert_not_called()
                sender.assert_not_called()
                email_process.assert_not_called()
                self.assertNotEqual(current, task.read_text(encoding="utf-8"))

    def test_replays_prepared_transition_after_task_replace_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            state = Path(tmp) / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, _current, constants, _claims, _unrelated = self.fixture(root, state)
                with (
                    patch.multiple(omo_completion_email, **constants),  # pyright: ignore[reportCallIssue, reportArgumentType]
                    patch("omo_manager.omo_pending.current_pending_task", return_value=task),
                    patch("omo_manager.omo_completion_email.current_pending_task", return_value=task),
                    patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True) as sent,
                    patch("omo_manager.omo_completion_email.send_completion_email") as sender,
                    redirect_stdout(StringIO()),
                ):
                    args = omo_pending.parse_args(["recover-source2003-pangram"])
                    with patch("omo_manager.omo_pending.fsync_task_parent", side_effect=OSError("crash after replace")):
                        with self.assertRaisesRegex(OSError, "crash after replace"):
                            omo_pending.run(args, root=root)
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    self.assertEqual(omo_completion_email.SOURCE2003_ITEMS, metadata.pending_task_items)
                    transition = next((state / "ordinary-pending-transitions").iterdir())
                    self.assertIn('"status":"prepared"', transition.read_text(encoding="utf-8"))
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    self.assertIn('"status":"committed"', transition.read_text(encoding="utf-8"))
                sent.assert_called_once()
                sender.assert_not_called()

    def test_rejects_task_source_sent_and_claim_drift_before_task_mutation(self) -> None:
        for drift in ("task", "source", "sent", "claim"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "work_logs"
                root.mkdir()
                state = Path(tmp) / "state"
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                    task, current, constants, claims, _unrelated = self.fixture(root, state)
                    if drift == "task":
                        task.write_text(current + "drift\n", encoding="utf-8")
                    elif drift == "source":
                        source = constants["SOURCE2003_REPORT_DRAFT_PATH"]
                        assert isinstance(source, Path)
                        source.write_text(source.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
                    elif drift == "claim":
                        authorization = state / "completion-email-authorizations" / claims[0]
                        authorization.write_text(
                            authorization.read_text(encoding="utf-8") + "drift=1\n",
                            encoding="utf-8",
                        )
                    task_before = task.read_bytes()
                    ledger = state / "completion-email-claims.tsv"
                    claims_before = ledger.read_bytes()
                    with (
                        patch.multiple(omo_completion_email, **constants),  # pyright: ignore[reportCallIssue, reportArgumentType]
                        patch("omo_manager.omo_pending.current_pending_task", return_value=task),
                        patch("omo_manager.omo_completion_email.current_pending_task", return_value=task),
                        patch(
                            "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent",
                            return_value=drift != "sent",
                        ),
                        patch("omo_manager.omo_completion_email.send_completion_email") as sender,
                        redirect_stdout(StringIO()),
                    ):
                        error = "task bytes changed|report replay does not bind|Sent-Mail evidence|claim evidence changed|authorization is malformed"
                        with self.assertRaisesRegex((OSError, BlockingError), error):
                            omo_pending.run(omo_pending.parse_args(["recover-source2003-pangram"]), root=root)
                    self.assertEqual(task_before, task.read_bytes())
                    self.assertEqual(claims_before, ledger.read_bytes())
                    sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
