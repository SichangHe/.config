from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_pending_watch as watcher
from omo_manager.omo_report_reconcile import ReconcileError, canonical_json, discard_commitment, reconcile


class ReportReconcileTests(unittest.TestCase):
    def discard(self, args: Namespace) -> dict[str, object]:
        with patch("omo_manager.omo_report_reconcile.LOCAL_ENV_PATH", args.local_env):
            return discard_commitment(args)

    def fixture(self, root: Path) -> Namespace:
        repo = root / "logs"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        manager_path = "manager.md"
        manager = repo / manager_path
        owner = b"owner bytes\n"
        envelope = root / "agent_blocked_hash.md"
        message = b"body\n"
        message_sha = hashlib.sha256(message).hexdigest()
        manager_digest = hashlib.sha256(str(manager).encode()).hexdigest()
        envelope.write_bytes((
            f"(sent from agent via omo_report.sh tmux=wl:18 time=now task-file=producer.md)\n"
            f"[message-sha256: {message_sha}]\n"
            f"[omo-report-owner-prefix: manager-path-sha256={manager_digest} sha256={hashlib.sha256(owner).hexdigest()} size-bytes={len(owner)} separator-bytes=1]\n"
            "message:\nbody\n"
        ).encode())
        envelope.chmod(0o600)
        pointer = f"(from agent wl:18 {envelope})".encode()
        before = owner + b"\n(pending)\n" + pointer + b"\n"
        manager.write_bytes(before)
        subprocess.run(["git", "add", manager_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "before"], cwd=repo, check=True)
        before_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        manager.write_bytes(owner)
        subprocess.run(["git", "add", manager_path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "after"], cwd=repo, check=True)
        after_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        receipts = root / "receipts"
        receipts.mkdir(mode=0o700)
        replay_id = "a" * 64
        producer = repo / "producer.md"
        producer.write_text("producer\n")
        unsigned = {
            "allocation": {"file_at_submission": {"sha256": message_sha, "size": len(message)}},
            "commitment": {},
            "preflight": {
                "owner_prefix": {
                    "manager_path_sha256": hashlib.sha256(str(manager).encode()).hexdigest(),
                    "separator_bytes": 1,
                    "sha256": hashlib.sha256(owner).hexdigest(),
                    "size_bytes": len(owner),
                },
                "records": {
                    "acknowledgment_ledger": str(root / "ack.tsv"),
                    "authority_completion": str(root / "authority.complete"),
                    "manager": str(manager),
                    "private_envelope": str(envelope),
                    "private_receipt": str(receipts / f"{replay_id}.json"),
                    "producer": str(producer),
                    "receipt_publication": str(receipts / f"{replay_id}.publication.json"),
                    "transaction_commitment": str(receipts / f"{replay_id}.commitment"),
                },
            },
            "replay_id": replay_id,
            "schema": "omo-report-transaction-commitment/v1",
        }
        commitment = {**unsigned, "commitment_id": hashlib.sha256(canonical_json(unsigned).rstrip(b"\n")).hexdigest()}
        commitment_path = receipts / f"{replay_id}.commitment"
        commitment_path.write_bytes(canonical_json(commitment))
        commitment_path.chmod(0o600)
        authorization_root = root / "trusted"
        authorization = authorization_root / "manager_mail" / "example.txt"
        authorization.parent.mkdir(parents=True)
        authorization.write_text(f"Replay {replay_id}: Consider it done, and make it go away.\n")
        local_env = root / "local.env"
        local_env.write_text(f'export OMO_WORK_LOGS_ROOT="{authorization_root}"\n')
        return Namespace(
            receipt_directory=receipts,
            replay_id=replay_id,
            report_key_sha256="b" * 64,
            repo=repo,
            manager_path=manager_path,
            before_revision=before_revision,
            after_revision=after_revision,
            envelope=envelope,
            producer_target="wl:18",
            commitment_id=commitment["commitment_id"],
            authorization_source="manager_mail/example.txt",
            authorization_file=authorization,
            local_env=local_env,
            authorization_sha256=hashlib.sha256(authorization.read_bytes()).hexdigest(),
        )

    def test_exact_human_authorized_discard_retires_only_commitment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            result = self.discard(args)
            self.assertEqual(result, self.discard(args))
            self.assertFalse((args.receipt_directory / f"{args.replay_id}.commitment").exists())
            disposition = args.receipt_directory / f"{args.replay_id}.discarded.json"
            self.assertEqual(0o600, disposition.stat().st_mode & 0o777)
            self.assertEqual("omo-report-authorized-discard/v1", result["schema"])

    def test_discard_rejects_wrong_commitment_identity_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            args.commitment_id = "0" * 64
            with self.assertRaisesRegex(ReconcileError, "differs"):
                self.discard(args)
            self.assertTrue((args.receipt_directory / f"{args.replay_id}.commitment").exists())
            self.assertFalse((args.receipt_directory / f"{args.replay_id}.discarded.json").exists())

    def test_discard_rejects_missing_manager_lock_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            commitment_path = args.receipt_directory / f"{args.replay_id}.commitment"
            commitment = json.loads(commitment_path.read_bytes())
            commitment["preflight"]["records"]["manager"] = None
            unsigned = {key: value for key, value in commitment.items() if key != "commitment_id"}
            commitment["commitment_id"] = hashlib.sha256(canonical_json(unsigned).rstrip(b"\n")).hexdigest()
            args.commitment_id = commitment["commitment_id"]
            commitment_path.write_bytes(canonical_json(commitment))
            with self.assertRaisesRegex(ReconcileError, "manager lock binding"):
                self.discard(args)
            self.assertFalse((args.receipt_directory / f"{args.replay_id}.discarded.json").exists())

    def test_discard_rejects_existing_receipt_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            (args.receipt_directory / f"{args.replay_id}.json").write_text("receipt")
            with self.assertRaisesRegex(ReconcileError, "terminal receipt"):
                self.discard(args)
            self.assertTrue((args.receipt_directory / f"{args.replay_id}.commitment").exists())

    def test_discard_rejects_non_mail_authority_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            args.authorization_source = "agent report"
            with self.assertRaisesRegex(ReconcileError, "authorization source"):
                self.discard(args)
            self.assertTrue((args.receipt_directory / f"{args.replay_id}.commitment").exists())

    def test_discard_rejects_fabricated_or_changed_authority_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            args.authorization_file.write_text("changed")
            with self.assertRaisesRegex(ReconcileError, "exact human disposition"):
                self.discard(args)
            self.assertTrue((args.receipt_directory / f"{args.replay_id}.commitment").exists())

    def test_discard_rejects_writable_or_symlinked_authority_without_mutation(self) -> None:
        for case in ("writable", "symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                args = self.fixture(Path(tmp))
                if case == "writable":
                    args.authorization_file.chmod(0o660)
                else:
                    payload = args.authorization_file.read_bytes()
                    external = Path(tmp) / "external.txt"
                    external.write_bytes(payload)
                    args.authorization_file.unlink()
                    args.authorization_file.symlink_to(external)
                with self.assertRaisesRegex(ReconcileError, "authorization source"):
                    self.discard(args)
                self.assertTrue((args.receipt_directory / f"{args.replay_id}.commitment").exists())
                self.assertFalse((args.receipt_directory / f"{args.replay_id}.discarded.json").exists())

    def test_discard_rejects_symlinked_commitment_without_detached_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            commitment = args.receipt_directory / f"{args.replay_id}.commitment"
            external = Path(tmp) / "external.commitment"
            commitment.replace(external)
            commitment.symlink_to(external)
            with self.assertRaisesRegex(ReconcileError, "commitment"):
                self.discard(args)
            self.assertTrue(external.exists())
            self.assertTrue(commitment.is_symlink())
            self.assertFalse((args.receipt_directory / f"{args.replay_id}.discarded.json").exists())

    def test_discard_rejects_mail_outside_trusted_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            args.local_env.write_text(f'export OMO_WORK_LOGS_ROOT="{Path(tmp) / "other"}"\n')
            with patch("omo_manager.omo_report_reconcile.LOCAL_ENV_PATH", args.local_env):
                with self.assertRaisesRegex(ReconcileError, "authorization source"):
                    self.discard(args)
            self.assertTrue((args.receipt_directory / f"{args.replay_id}.commitment").exists())

    def test_discard_rejects_symlinked_configured_mail_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            mail_root = args.authorization_file.parent
            external = Path(tmp) / "external-manager-mail"
            mail_root.replace(external)
            mail_root.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ReconcileError, "authorization source"):
                self.discard(args)
            self.assertTrue((args.receipt_directory / f"{args.replay_id}.commitment").exists())
            self.assertFalse((args.receipt_directory / f"{args.replay_id}.discarded.json").exists())

    def test_historical_reconciliation_rejects_discard_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            disposition = args.receipt_directory / f"{args.replay_id}.discarded.json"
            disposition.symlink_to(args.receipt_directory / "missing-disposition")
            with self.assertRaisesRegex(ReconcileError, "explicitly discarded"):
                reconcile(args)
            self.assertFalse((args.receipt_directory / f"{args.replay_id}.tombstone.json").exists())

    def test_discard_persists_disposition_before_commitment_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            commitment_path = args.receipt_directory / f"{args.replay_id}.commitment"
            original_unlink = Path.unlink

            def interrupt_commitment(path: Path, *call_args: object, **call_kwargs: object) -> None:
                if path == commitment_path:
                    raise OSError("interrupted before commitment retirement")
                original_unlink(path, *call_args, **call_kwargs)

            with patch.object(Path, "unlink", autospec=True, side_effect=interrupt_commitment):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    self.discard(args)
            self.assertTrue(commitment_path.exists())
            self.assertTrue((args.receipt_directory / f"{args.replay_id}.discarded.json").exists())
            self.assertEqual("omo-report-authorized-discard/v1", self.discard(args)["schema"])
            self.assertFalse(commitment_path.exists())

    def test_receipt_layout_rejects_terminal_discard(self) -> None:
        source = Path(__file__).parents[1] / "omo_report_receipt.py"
        text = source.read_text()
        self.assertIn('f"{plan.replay_id}.discarded.json"', text)
        self.assertIn("report transaction was explicitly discarded", text)

    def test_exact_historical_clear_creates_idempotent_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            first = reconcile(args)
            second = reconcile(args)
            self.assertEqual(first, second)
            self.assertTrue(first["terminal"])
            self.assertEqual("omo-report-historical-clear-tombstone/v1", first["schema"])

    def test_unrelated_historical_after_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            manager = args.repo / args.manager_path
            manager.write_bytes(b"different\n")
            subprocess.run(["git", "add", args.manager_path], cwd=args.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "different"], cwd=args.repo, check=True)
            args.after_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.repo, text=True).strip()
            with self.assertRaisesRegex(ReconcileError, "owner bytes"):
                reconcile(args)

    def test_historical_reconciliation_rejects_dangling_receipt_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            (args.receipt_directory / f"{args.replay_id}.json").symlink_to(args.receipt_directory / "missing-receipt")
            with self.assertRaisesRegex(ReconcileError, "durable receipt"):
                reconcile(args)
            self.assertFalse((args.receipt_directory / f"{args.replay_id}.tombstone.json").exists())

    def test_commitment_owner_must_name_exact_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.fixture(Path(tmp))
            commitment_path = args.receipt_directory / f"{args.replay_id}.commitment"
            commitment = json.loads(commitment_path.read_bytes())
            commitment["preflight"]["owner_prefix"]["manager_path_sha256"] = "0" * 64
            unsigned = {key: value for key, value in commitment.items() if key != "commitment_id"}
            commitment["commitment_id"] = hashlib.sha256(canonical_json(unsigned).rstrip(b"\n")).hexdigest()
            commitment_path.write_bytes(canonical_json(commitment))
            with self.assertRaisesRegex(ReconcileError, "owner-prefix"):
                reconcile(args)

    def test_watcher_accepts_only_exact_manager_pointer_and_report_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            manager = logs / "manager.md"
            manager.write_text("owner\n")
            envelope = root / "agent_blocked_hash.md"
            manager_digest = hashlib.sha256(str(manager.resolve()).encode()).hexdigest()
            owner_digest = "2" * 64
            envelope.write_text(
                f"[omo-report-owner-prefix: manager-path-sha256={manager_digest} sha256={owner_digest} size-bytes=1 separator-bytes=1]\n"
                "message:\nbody\n"
            )
            source = f"(from agent wl:18 {envelope})"
            marker = watcher.Marker(Path("manager.md"), 1, "digest", "agent", source, "", f"(pending)\n{source}", source, 2, "")
            args = watcher.Args(logs, "", root / "seen.tsv", 1, 1, 1, root / "status", False, False)
            report_key = f"{logs}:agent-report:" + "1" * 64
            state_home = root / "state"
            receipt_dir = state_home / "omo-manager" / "report-receipts"
            receipt_dir.mkdir(parents=True)
            receipt_dir.chmod(0o700)

            def record(*, key: str = report_key, manager_path: Path = manager, pointer: str = source) -> dict[str, object]:
                unsigned: dict[str, object] = {
                    "envelope_path": str(envelope.resolve()),
                    "envelope_sha256": hashlib.sha256(envelope.read_bytes()).hexdigest(),
                    "manager_path": str(manager_path.resolve()),
                    "manager_path_sha256": hashlib.sha256(str(manager_path.resolve()).encode()).hexdigest(),
                    "owner_prefix_sha256": owner_digest,
                    "pointer_sha256": hashlib.sha256(pointer.encode()).hexdigest(),
                    "report_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                    "schema": "omo-report-historical-clear-tombstone/v1",
                    "terminal": True,
                }
                return {**unsigned, "tombstone_id": hashlib.sha256(canonical_json(unsigned).rstrip(b"\n")).hexdigest()}

            with patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}), patch.object(
                watcher, "agent_report_source", return_value=str(envelope)
            ):
                (receipt_dir / "bare.commitment").write_text("commitment")
                (receipt_dir / "prose.tombstone.json").write_text("completed producer")
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                (receipt_dir / "wrong.tombstone.json").write_bytes(canonical_json(record(key="wrong")))
                (receipt_dir / "wrong.tombstone.json").chmod(0o600)
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                (receipt_dir / "wrong.tombstone.json").write_bytes(canonical_json(record(manager_path=logs / "other.md")))
                (receipt_dir / "wrong.tombstone.json").chmod(0o600)
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                (receipt_dir / "wrong.tombstone.json").write_bytes(canonical_json(record(pointer="other pointer")))
                (receipt_dir / "wrong.tombstone.json").chmod(0o600)
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                exact = canonical_json(record())
                (receipt_dir / "exact.tombstone.json").write_bytes(exact)
                (receipt_dir / "exact.tombstone.json").chmod(0o600)
                self.assertTrue(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                (receipt_dir / "exact.tombstone.json").chmod(0o644)
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                (receipt_dir / "exact.tombstone.json").unlink()
                (receipt_dir / "real.json").write_bytes(exact)
                (receipt_dir / "real.json").chmod(0o600)
                (receipt_dir / "exact.tombstone.json").symlink_to(receipt_dir / "real.json")
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                (receipt_dir / "exact.tombstone.json").unlink()
                (receipt_dir / "exact.tombstone.json").write_bytes(exact)
                (receipt_dir / "exact.tombstone.json").chmod(0o600)
                envelope.write_text(
                    f"[omo-report-owner-prefix: manager-path-sha256={manager_digest} sha256={'3' * 64} size-bytes=1 separator-bytes=1]\n"
                    "message:\nbody\n"
                )
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                envelope.write_text("message:\nbody\n")
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))
                envelope.write_text(
                    f"[omo-report-owner-prefix: manager-path-sha256={manager_digest} sha256={owner_digest} size-bytes=1 separator-bytes=1]\n"
                    "message:\nbody\n"
                )
                (receipt_dir / "exact.tombstone.json").write_bytes(exact[:-2] + b"0\n")
                self.assertFalse(watcher.report_has_historical_clear_tombstone(args, marker, report_key))


if __name__ == "__main__":
    unittest.main()
