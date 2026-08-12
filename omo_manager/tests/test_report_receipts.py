from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path


OMO_DIR = Path(__file__).resolve().parents[1]
REPORT = OMO_DIR / "omo_report.sh"


@dataclass(frozen=True)
class ReportFixture:
    root: Path
    message: Path
    env: dict[str, str]

    @property
    def manager(self) -> Path:
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        return self.root / f"work_manager_{day}.md"

    def command(
        self,
        *,
        describe: bool = False,
        verify_consumed: bool = False,
        status: str = "done",
    ) -> list[str]:
        command = [str(REPORT)]
        if describe:
            command.append("--describe")
        if verify_consumed:
            command.append("--verify-consumed")
        return command + ["--status", status, "--message-file", str(self.message), "--agent", "receipt-worker"]


def frontmatter(*, runat: str, managerat: str, is_manager: bool = False) -> str:
    return "\n".join(
        [
            "---",
            "version: v1.0.0",
            "status: running",
            f"runat: {runat}",
            "tool: codex",
            f"managerat: {managerat}",
            f"is_manager: {str(is_manager).lower()}",
            "pending_task_items: []",
            "---",
            "",
        ]
    )


def fixture(tmp_path: Path, *, body: bytes = b"private report\n", managerat: str = "main:0.0") -> ReportFixture:
    root = tmp_path / "logs"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(
        """#!/usr/bin/env bash
printf 'cfg\\t7\\t0\\t%%1701\\tworker\\n'
""",
        encoding="utf-8",
    )
    tmux.chmod(0o700)
    local_env = tmp_path / "local.env"
    local_env.write_text(
        f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:0.0\n",
        encoding="utf-8",
    )
    (root / "TODO.md").write_text("current:\nworker.md cfg:7\n", encoding="utf-8")
    (root / "worker.md").write_text(frontmatter(runat="cfg:7", managerat=managerat), encoding="utf-8")
    message = tmp_path / "message.md"
    message.write_bytes(body)
    message.chmod(0o600)
    env = dict(os.environ)
    for name in ("OMO_WORK_LOGS_ROOT", "OMO_MANAGER_TMUX_TARGET", "OMO_AGENT_NAME", "TMUX"):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "OMO_MANAGER_LOCAL_ENV": str(local_env),
            "OMO_REPORT_ACK_TIMEOUT_S": "2",
            "PATH": f"{bin_dir}:{env['PATH']}",
            "TMUX_PANE": "%1701",
        }
    )
    return ReportFixture(root=root, message=message, env=env)


def active_manager_fixture(tmp_path: Path, *, body: bytes = b"unused active-route body\n") -> tuple[ReportFixture, Path, bytes]:
    case = fixture(tmp_path, body=body, managerat="vl:2")
    manager = case.root / "manager.md"
    (case.root / "TODO.md").write_text(
        "current:\nworker.md cfg:7\nmanager.md vl:2\n",
        encoding="utf-8",
    )
    owner = frontmatter(runat="vl:2", managerat="main:0.0", is_manager=True).encode()
    manager.write_bytes(owner)
    return case, manager, owner


def run_report(
    case: ReportFixture,
    *,
    describe: bool = False,
    verify_consumed: bool = False,
    status: str = "done",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        case.command(describe=describe, verify_consumed=verify_consumed, status=status),
        cwd=case.root.parent,
        env=case.env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def allocate_report_draft(case: ReportFixture, body: bytes) -> Path:
    result = subprocess.run(
        [str(REPORT), "--alloc-message-file"],
        cwd=case.root.parent,
        env=case.env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    draft = Path(result.stdout.strip())
    draft.write_bytes(body)
    return draft


def run_report_from(
    case: ReportFixture,
    message: Path,
    *,
    describe: bool = False,
    verify_consumed: bool = False,
    status: str = "done",
    report: Path = REPORT,
) -> subprocess.CompletedProcess[str]:
    command = replace(case, message=message).command(
        describe=describe,
        verify_consumed=verify_consumed,
        status=status,
    )
    command[0] = str(report)
    return subprocess.run(
        command,
        cwd=case.root.parent,
        env=case.env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def transaction_commitment_path(description: dict[str, object]) -> Path:
    files = description["files"]
    assert isinstance(files, dict)
    return Path(str(files["private_receipt"])).with_suffix(".commitment")


def path_set(payload: dict[str, object]) -> set[str]:
    paths: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"path", "directory", "temporary", "final"} and isinstance(item, str) and item.startswith("/"):
                    paths.add(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload.get("side_effects"))
    receipt = payload.get("receipt") or payload.get("receipt_record")
    if isinstance(receipt, dict):
        collect(receipt)
    return paths


def cleanup_private_tmp(payload: dict[str, object]) -> None:
    for raw_path in path_set(payload):
        path = Path(raw_path)
        if path.is_file() and (str(path).startswith(f"/tmp/omo-agent-messages-{os.getuid()}/") or str(path).startswith(f"/tmp/omo-task-file-locks-{os.getuid()}/")):
            path.unlink()


def private_receipt(acceptance: dict[str, object]) -> dict[str, object]:
    return json.loads(Path(str(acceptance["receipt_path"])).read_text(encoding="utf-8"))


def acknowledgment_coordinates(description: dict[str, object]) -> tuple[Path, str]:
    files = description["files"]
    routing = description["routing"]
    input_info = description["input"]
    assert isinstance(files, dict)
    assert isinstance(routing, dict)
    assert isinstance(input_info, dict)
    envelope = str(files["private_envelope"])
    root = Path(str(routing["task"])).parent.resolve()
    hash_line = f"[message-sha256: {input_info['sha256']}]"
    identity = f"{envelope}\0{envelope}\0{hash_line}"
    key = f"{root}:agent-report:{hashlib.sha256(identity.encode()).hexdigest()}"
    receipt = Path(str(files["private_receipt"]))
    state = receipt.parent.parent / "pending-watch-consumed-reports.tsv"
    return state, key


def run_accepted(case: ReportFixture, *, status: str = "done") -> tuple[dict[str, object], subprocess.CompletedProcess[str]]:
    description = json.loads(run_report(case, describe=True, status=status).stdout)
    case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
    pending = run_report(case, status=status)
    if pending.returncode != 0 or json.loads(pending.stdout).get("accepted") is not False:
        raise AssertionError(pending.stderr or pending.stdout)
    files = description["files"]
    assert isinstance(files, dict)
    watcher = run_manager_watcher_once(case, Path(str(files["manager"])))
    if watcher.returncode != 0:
        raise AssertionError(watcher.stderr)
    return description, run_report(case, status=status)


def run_manager_watcher_once(case: ReportFixture, manager: Path | None = None) -> subprocess.CompletedProcess[str]:
    program = """
from pathlib import Path
import sys
from unittest.mock import patch
from omo_manager import omo_pending_watch as watcher

root = Path(sys.argv[1])
manager = Path(sys.argv[2])
args = watcher.parse_args(["--root", str(root), "--once"])
with patch.object(watcher, "push_marker_delivery", return_value=watcher.DeliveryResult(0)) as push:
    if not watcher.scan_once(args, {}, [manager]):
        raise SystemExit("watcher did not consume the report")
if push.call_count != 1:
    raise SystemExit(f"watcher delivered {push.call_count} reports")
print(push.call_args.args[3])
"""
    selected_manager = manager or case.manager
    return subprocess.run(
        [sys.executable, "-c", program, str(case.root), str(selected_manager)],
        cwd=OMO_DIR.parent,
        env={
            **case.env,
            "OMO_MANAGER_TMUX_TARGET": "main:0.0",
            "OMO_WORK_LOGS_ROOT": str(case.root),
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def copy_report_helper(tmp_path: Path) -> Path:
    package = tmp_path / "copied-config" / "omo_manager"
    package.mkdir(parents=True)
    for name in ("omo_pending_digest.py", "omo_report.sh", "omo_report_receipt.py", "omo_task_lock.py"):
        shutil.copy2(OMO_DIR / name, package / name)
    report = package / "omo_report.sh"
    report.chmod(0o700)
    return report


def entry_name_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for name in sorted(os.listdir(os.fsencode(path))):
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
    return digest.hexdigest()


def preflight_paths(preflight: dict[str, object]) -> set[str]:
    allocation = preflight["allocation"]
    locks = preflight["locks"]
    records = preflight["records"]
    assert isinstance(allocation, dict) and isinstance(locks, dict) and isinstance(records, dict)
    paths = {str(allocation["directory"]), str(allocation["file"])}
    paths.update(str(path) for path in preflight["directories"])  # type: ignore[union-attr]
    paths.update(str(path) for path in preflight["temporary_files"])  # type: ignore[union-attr]
    paths.update(str(path) for path in records.values())
    paths.update(
        str(locks[key])
        for key in ("acknowledgment", "allocation_replay", "adjacent_report", "watcher_authority")
    )
    route_locks = locks["routing"]
    assert isinstance(route_locks, list)
    paths.update(str(item["path"]) for item in route_locks)
    routing_sources = preflight["routing_sources"]
    assert isinstance(routing_sources, list)
    paths.update(str(item["path"]) for item in routing_sources)
    return paths


def side_effect_paths(effects: dict[str, object]) -> set[str]:
    path_keys = {
        "application_directory",
        "authority_completion",
        "authority_directory",
        "authority_lock",
        "directory",
        "file",
        "final",
        "lock",
        "maintenance_temporary",
        "path",
        "state",
        "state_home",
        "temporary",
        "watcher_temporary",
    }
    paths: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in path_keys and isinstance(item, str) and item.startswith("/"):
                    paths.add(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(effects)
    return paths


class ReportReceiptTests(unittest.TestCase):
    def test_watcher_canonicalizes_state_home_for_commitment_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case, manager, owner = active_manager_fixture(tmp_path, body=b"canonical state home\n")
            (tmp_path / "state-parent").mkdir()
            case.env["XDG_STATE_HOME"] = str(tmp_path / "state-parent" / ".." / "state")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report(case)
            self.assertEqual(0, pending.returncode, pending.stderr)
            watched = run_manager_watcher_once(case, manager)
            self.assertEqual(0, watched.returncode, watched.stderr)
            self.assertEqual(owner, manager.read_bytes())

    def test_legacy_v1_pending_retry_publication_and_consumed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case, manager, _owner = active_manager_fixture(Path(tmp), body=b"legacy replay\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            description = json.loads(run_report(case, describe=True).stdout)
            pending = run_report(case)
            self.assertEqual(0, pending.returncode, pending.stderr)
            commitment_path = transaction_commitment_path(description)
            commitment = json.loads(commitment_path.read_bytes())
            commitment.pop("transfer")
            commitment["schema"] = "omo-report-transaction-commitment/v1"
            commitment.pop("commitment_id")
            commitment["commitment_id"] = hashlib.sha256(
                json.dumps(commitment, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            commitment_path.write_bytes((json.dumps(commitment, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode())
            envelope = Path(str(description["files"]["private_envelope"]))
            lines = envelope.read_text().splitlines(keepends=True)
            envelope.write_text("".join(line for line in lines if not line.startswith("[omo-transfer: ")))

            retried = run_report(case)
            self.assertEqual(0, retried.returncode, retried.stderr)
            legacy_transfer = json.loads(retried.stdout)["transfer_receipt"]
            self.assertEqual("omo-report-transfer-receipt/legacy-v1", legacy_transfer["schema"])
            watched = run_manager_watcher_once(case, manager)
            self.assertEqual(0, watched.returncode, watched.stderr)
            accepted = run_report(case)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            accepted_output = json.loads(accepted.stdout)
            self.assertEqual(legacy_transfer, accepted_output["transfer_receipt"])
            self.assertTrue(Path(accepted_output["publication_path"]).is_file())

    def test_transfer_envelope_capacity_preflight_leaves_no_commitment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"x")
            low, high = 1, 200_000
            while low + 1 < high:
                middle = (low + high) // 2
                case.message.write_bytes(b"x" * middle)
                result = run_report(case, describe=True)
                if result.returncode == 0:
                    low = middle
                else:
                    high = middle
            case.message.write_bytes(b"x" * high)
            rejected = run_report(case)
            self.assertEqual(2, rejected.returncode)
            self.assertIn("envelope exceeds", rejected.stderr)
            receipt_dir = Path(case.env["XDG_STATE_HOME"]) / "omo-manager" / "report-receipts"
            self.assertEqual([], list(receipt_dir.glob("*.commitment")) if receipt_dir.exists() else [])

    def test_watcher_rejects_self_consistent_tampered_transfer_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case, manager, owner = active_manager_fixture(Path(tmp), body=b"tamper transfer\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            description = json.loads(run_report(case, describe=True).stdout)
            self.assertEqual(0, run_report(case).returncode)
            envelope = Path(str(description["files"]["private_envelope"]))
            lines = envelope.read_text().splitlines(keepends=True)
            index = next(i for i, line in enumerate(lines) if line.startswith("[omo-transfer: "))
            transfer = json.loads(lines[index][len("[omo-transfer: ") : -2])
            transfer["routing"]["route_kind"] = "forged-but-not-test-sentinel"
            unsigned = {key: value for key, value in transfer.items() if key != "transfer_id"}
            transfer["transfer_id"] = hashlib.sha256(
                json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            lines[index] = f"[omo-transfer: {json.dumps(transfer, ensure_ascii=True, sort_keys=True, separators=(',', ':'))}]\n"
            envelope.write_text("".join(lines))
            watched = run_manager_watcher_once(case, manager)
            self.assertNotEqual(0, watched.returncode)
            self.assertIn("did not consume", watched.stderr)
            self.assertNotEqual(owner, manager.read_bytes())
            self.assertIn("(pending)", manager.read_text())

    def test_pending_and_accepted_share_stable_transfer_receipt_with_agent_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case, manager, owner = active_manager_fixture(tmp_path, body=b"queue transfer receipt\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            described = run_report(case, describe=True, status="progressing")
            self.assertEqual(0, described.returncode, described.stderr)
            description = json.loads(described.stdout)
            pending = run_report(case, status="progressing")
            self.assertEqual(0, pending.returncode, pending.stderr)
            pending_output = json.loads(pending.stdout)
            transfer = pending_output["transfer_receipt"]
            self.assertFalse(pending_output["accepted"])
            self.assertEqual("omo-report-transfer-receipt/v1", transfer["schema"])
            self.assertEqual("agent-originated", transfer["authority"]["kind"])
            self.assertEqual(str(case.root / "worker.md"), transfer["authority"]["source_task"])
            self.assertEqual(str(manager), transfer["receiver"])
            self.assertEqual(str(transaction_commitment_path(description)), transfer["commitment_path"])
            unsigned = {key: value for key, value in transfer.items() if key != "transfer_id"}
            self.assertEqual(hashlib.sha256(json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), transfer["transfer_id"])

            watched = run_manager_watcher_once(case, manager)
            self.assertEqual(0, watched.returncode, watched.stderr)
            self.assertEqual(owner, manager.read_bytes())
            accepted = run_report(case, status="progressing")
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            accepted_output = json.loads(accepted.stdout)
            self.assertTrue(accepted_output["accepted"])
            self.assertEqual(transfer, accepted_output["transfer_receipt"])

    def test_public_allocated_draft_rejects_then_accepts_with_exact_owner_restoration(self) -> None:
        owners = (b"", b"owner with terminal newline\n", b"owner without terminal newline")
        for owner in owners:
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as tmp:
                case = fixture(Path(tmp), body=b"unused fixture body\n")
                case.manager.write_bytes(owner)
                allocated = subprocess.run(
                    [str(REPORT), "--alloc-message-file"],
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(0, allocated.returncode, allocated.stderr)
                draft = Path(allocated.stdout.strip())
                self.addCleanup(draft.unlink, missing_ok=True)
                draft.write_bytes(b"installed public helper compatibility\n")
                case = replace(case, message=draft)

                described = run_report(case, describe=True)
                self.assertEqual(0, described.returncode, described.stderr)
                description = json.loads(described.stdout)
                self.assertNotIn(str(draft), described.stdout)
                case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
                pending = run_report(case)
                self.assertEqual(0, pending.returncode, pending.stderr)
                self.assertFalse(json.loads(pending.stdout)["accepted"])
                self.assertNotEqual(owner, case.manager.read_bytes())

                watcher = run_manager_watcher_once(case)
                self.assertEqual(0, watcher.returncode, watcher.stderr)
                self.assertEqual(owner, case.manager.read_bytes())
                accepted = run_report(case)
                self.assertEqual(0, accepted.returncode, accepted.stderr)
                acceptance = json.loads(accepted.stdout)
                self.assertTrue(acceptance["accepted"])
                receipt = private_receipt(acceptance)
                self.addCleanup(cleanup_private_tmp, receipt)
                self.assertEqual(description["transaction"]["sha256"], receipt["preflight"]["sha256"])
                self.assertEqual(description["transaction"]["owner_prefix"], receipt["preflight"]["owner_prefix"])
                self.assertEqual(owner, case.manager.read_bytes())
                self.assertEqual(accepted.stdout, run_report(case).stdout)

    def test_pending_rebind_rejects_b_and_preserves_first_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            owner = b"exact owner bytes without newline"
            case.manager.write_bytes(owner)
            body = b"pending A/B body must stay private\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            self.addCleanup(draft_b.unlink, missing_ok=True)
            description_a = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"

            pending_a = run_report_from(case, draft_a)

            self.assertEqual(0, pending_a.returncode, pending_a.stderr)
            self.assertFalse(json.loads(pending_a.stdout)["accepted"])
            commitment_path = transaction_commitment_path(description_a)
            commitment_bytes = commitment_path.read_bytes()
            commitment = json.loads(commitment_bytes)
            manager_pending = case.manager.read_bytes()
            receipt_path = Path(str(description_a["files"]["private_receipt"]))
            publication_path = Path(str(description_a["files"]["receipt_publication"]))
            self.assertEqual(str(draft_a), commitment["preflight"]["allocation"]["file"])
            self.assertEqual(description_a["transaction"]["sha256"], commitment["preflight"]["sha256"])
            self.assertEqual(0o600, commitment_path.stat().st_mode & 0o777)

            described_b = run_report_from(case, draft_b, describe=True)
            pending_b = run_report_from(case, draft_b)

            for rejected in (described_b, pending_b):
                self.assertEqual(2, rejected.returncode)
                self.assertEqual("", rejected.stdout)
                self.assertIn("bound to a different allocation", rejected.stderr)
                self.assertNotIn(body.decode().strip(), rejected.stderr)
                self.assertNotIn(str(draft_a), rejected.stderr)
                self.assertNotIn(str(draft_b), rejected.stderr)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(manager_pending, case.manager.read_bytes())
            self.assertFalse(receipt_path.exists())
            self.assertFalse(publication_path.exists())

            watched = run_manager_watcher_once(case)
            self.assertEqual(0, watched.returncode, watched.stderr)
            self.assertEqual(owner, case.manager.read_bytes())
            rejected_after_watch = run_report_from(case, draft_b)
            self.assertEqual(2, rejected_after_watch.returncode)
            self.assertIn("bound to a different allocation", rejected_after_watch.stderr)
            accepted_a = run_report_from(case, draft_a)
            self.assertEqual(0, accepted_a.returncode, accepted_a.stderr)
            receipt = private_receipt(json.loads(accepted_a.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual(str(draft_a), receipt["preflight"]["allocation"]["file"])
            self.assertNotIn(str(draft_b), json.dumps(receipt, sort_keys=True))
            self.assertLessEqual(side_effect_paths(receipt["side_effects"]), preflight_paths(receipt["preflight"]))
            self.assertTrue(all(not Path(path).exists() for path in receipt["preflight"]["temporary_files"]))
            draft_b_lock = receipt_path.parent / f".allocation-{hashlib.sha256(str(draft_b).encode()).hexdigest()}.lock"
            draft_b_lock.write_bytes(b"")
            draft_b_lock.chmod(0o644)
            accepted_b = run_report_from(case, draft_b)
            self.assertEqual(accepted_a.stdout, accepted_b.stdout, accepted_b.stderr)
            self.assertEqual(owner, case.manager.read_bytes())
            records = list(receipt_path.parent.glob("*.json"))
            self.assertEqual(2, len(records))
            Path(str(receipt["preflight"]["locks"]["allocation_replay"])).unlink()
            deleted_lock_replay = run_report_from(case, draft_a)
            self.assertEqual(2, deleted_lock_replay.returncode)
            self.assertIn("allocation replay lock is inconsistent", deleted_lock_replay.stderr)

    def test_disappeared_draft_replacement_cannot_finish_after_watcher_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            owner = b"replacement retry owner\n"
            case.manager.write_bytes(owner)
            body = b"disappeared A replaced by B at A path\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            original_a = draft_a.with_name(f".{draft_a.name}.original")
            for draft in (draft_a, draft_b, original_a):
                self.addCleanup(draft.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft_a)
            self.assertEqual(0, pending.returncode, pending.stderr)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            commitment = json.loads(commitment_bytes)
            committed_inode = commitment["allocation"]["file_at_submission"]["inode"]
            envelope = Path(str(description["files"]["private_envelope"]))
            manager_pending = case.manager.read_bytes()
            envelope_bytes = envelope.read_bytes()
            receipt_path = Path(str(description["files"]["private_receipt"]))
            publication_path = Path(str(description["files"]["receipt_publication"]))
            os.link(draft_a, original_a)
            draft_a.unlink()
            os.replace(draft_b, draft_a)
            self.assertNotEqual(committed_inode, draft_a.stat().st_ino)

            rejected_description = run_report_from(case, draft_a, describe=True)
            rejected_pending = run_report_from(case, draft_a)

            for rejected in (rejected_description, rejected_pending):
                self.assertEqual(2, rejected.returncode)
                self.assertEqual("", rejected.stdout)
                self.assertIn("draft object identity differs", rejected.stderr)
                self.assertNotIn(body.decode().strip(), rejected.stderr)
                self.assertNotIn(str(draft_a), rejected.stderr)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(manager_pending, case.manager.read_bytes())
            self.assertEqual(envelope_bytes, envelope.read_bytes())
            self.assertFalse(receipt_path.exists())
            self.assertFalse(publication_path.exists())

            first_watcher = run_manager_watcher_once(case)
            restarted_watcher = run_manager_watcher_once(case)
            self.assertEqual(0, first_watcher.returncode, first_watcher.stderr)
            self.assertNotEqual(0, restarted_watcher.returncode)
            self.assertEqual(owner, case.manager.read_bytes())
            rejected_after_watch = run_report_from(case, draft_a)
            self.assertEqual(2, rejected_after_watch.returncode)
            self.assertIn("draft object identity differs", rejected_after_watch.stderr)
            self.assertFalse(receipt_path.exists())
            self.assertFalse(publication_path.exists())

            os.replace(original_a, draft_a)
            self.assertEqual(committed_inode, draft_a.stat().st_ino)
            accepted = run_report_from(case, draft_a)
            replay = run_report_from(case, draft_a)
            replay_draft = allocate_report_draft(case, body)
            self.addCleanup(replay_draft.unlink, missing_ok=True)
            replay_from_new_draft = run_report_from(case, replay_draft)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(accepted.stdout, replay.stdout)
            self.assertEqual(accepted.stdout, replay_from_new_draft.stdout)
            self.assertEqual(2, len(list(receipt_path.parent.glob("*.json"))))
            self.assertEqual(owner, case.manager.read_bytes())

    def test_unlink_recreate_at_committed_draft_path_is_not_the_same_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            body = b"unlink and recreate same bytes\n"
            draft_a = allocate_report_draft(case, body)
            original_a = draft_a.with_name(f".{draft_a.name}.original")
            for draft in (draft_a, original_a):
                self.addCleanup(draft.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft_a)
            self.assertEqual(0, pending.returncode, pending.stderr)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            manager_pending = case.manager.read_bytes()
            receipt_path = Path(str(description["files"]["private_receipt"]))
            publication_path = Path(str(description["files"]["receipt_publication"]))
            original_inode = draft_a.stat().st_ino
            os.link(draft_a, original_a)
            draft_a.unlink()
            draft_a.write_bytes(body)
            draft_a.chmod(0o600)
            self.assertNotEqual(original_inode, draft_a.stat().st_ino)

            rejected = run_report_from(case, draft_a)

            self.assertEqual(2, rejected.returncode)
            self.assertEqual("", rejected.stdout)
            self.assertIn("draft object identity differs", rejected.stderr)
            self.assertNotIn(body.decode().strip(), rejected.stderr)
            self.assertNotIn(str(draft_a), rejected.stderr)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(manager_pending, case.manager.read_bytes())
            self.assertFalse(receipt_path.exists())
            self.assertFalse(publication_path.exists())

    def test_concurrent_retries_reject_renamed_substitute_at_committed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            body = b"concurrent rename substitution\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            original_a = draft_a.with_name(f".{draft_a.name}.original")
            for draft in (draft_a, draft_b, original_a):
                self.addCleanup(draft.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft_a)
            self.assertEqual(0, pending.returncode, pending.stderr)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            manager_pending = case.manager.read_bytes()
            receipt_path = Path(str(description["files"]["private_receipt"]))
            publication_path = Path(str(description["files"]["receipt_publication"]))
            original_inode = draft_a.stat().st_ino
            os.link(draft_a, original_a)
            os.replace(draft_b, draft_a)
            self.assertNotEqual(original_inode, draft_a.stat().st_ino)
            processes = [
                subprocess.Popen(
                    replace(case, message=draft_a).command(),
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]

            results = [process.communicate(timeout=20) for process in processes]

            for process, (stdout, stderr) in zip(processes, results, strict=True):
                self.assertEqual(2, process.returncode)
                self.assertEqual("", stdout)
                self.assertIn("draft object identity differs", stderr)
                self.assertNotIn(body.decode().strip(), stderr)
                self.assertNotIn(str(draft_a), stderr)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(manager_pending, case.manager.read_bytes())
            self.assertFalse(receipt_path.exists())
            self.assertFalse(publication_path.exists())

    def test_pending_retry_uses_held_draft_after_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, body=b"unused\n")
            owner = b"snapshot race owner\n"
            case.manager.write_bytes(owner)
            body = b"rename after retry snapshot\n"
            draft_a = allocate_report_draft(case, body)
            replacement_body = b"replacement pathname must be ignored\n"
            draft_b = allocate_report_draft(case, replacement_body)
            original_a = draft_a.with_name(f".{draft_a.name}.original")
            for draft in (draft_a, draft_b, original_a):
                self.addCleanup(draft.unlink, missing_ok=True)
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = "def submit(plan: Plan) -> tuple[bytes, bytes] | None:\n    validate_helper_snapshot(plan)\n"
            injection = (
                "def submit(plan: Plan) -> tuple[bytes, bytes] | None:\n"
                '    replacement = os.environ.get("OMO_TEST_RENAME_AFTER_MESSAGE_SNAPSHOT")\n'
                "    if replacement:\n"
                "        os.replace(replacement, plan.message_path)\n"
                "    validate_helper_snapshot(plan)\n"
            )
            self.assertIn(marker, source)
            receiver.write_text(source.replace(marker, injection, 1), encoding="utf-8")
            description = json.loads(run_report_from(case, draft_a, describe=True, report=report).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft_a, report=report)
            self.assertEqual(0, pending.returncode, pending.stderr)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            manager_pending = case.manager.read_bytes()
            envelope = Path(str(description["files"]["private_envelope"]))
            envelope_bytes = envelope.read_bytes()
            receipt_path = Path(str(description["files"]["private_receipt"]))
            publication_path = Path(str(description["files"]["receipt_publication"]))
            os.link(draft_a, original_a)
            case.env["OMO_TEST_RENAME_AFTER_MESSAGE_SNAPSHOT"] = str(draft_b)

            continued = run_report_from(case, draft_a, report=report)

            case.env.pop("OMO_TEST_RENAME_AFTER_MESSAGE_SNAPSHOT")
            self.assertEqual(0, continued.returncode, continued.stderr)
            self.assertFalse(json.loads(continued.stdout)["accepted"])
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(manager_pending, case.manager.read_bytes())
            self.assertEqual(envelope_bytes, envelope.read_bytes())
            self.assertIn(body, envelope_bytes)
            self.assertNotIn(replacement_body, envelope_bytes)
            self.assertFalse(receipt_path.exists())
            self.assertFalse(publication_path.exists())

    def test_initial_submit_uses_held_draft_after_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, body=b"unused\n")
            body = b"initial held draft body\n"
            replacement_body = b"initial replacement pathname must be ignored\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, replacement_body)
            original_a = draft_a.with_name(f".{draft_a.name}.original")
            for draft in (draft_a, draft_b, original_a):
                self.addCleanup(draft.unlink, missing_ok=True)
            os.link(draft_a, original_a)
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = "def submit(plan: Plan) -> tuple[bytes, bytes] | None:\n    validate_helper_snapshot(plan)\n"
            injection = (
                "def submit(plan: Plan) -> tuple[bytes, bytes] | None:\n"
                '    replacement = os.environ.get("OMO_TEST_RENAME_AFTER_MESSAGE_SNAPSHOT")\n'
                "    if replacement:\n"
                "        os.replace(replacement, plan.message_path)\n"
                "    validate_helper_snapshot(plan)\n"
            )
            self.assertIn(marker, source)
            receiver.write_text(source.replace(marker, injection, 1), encoding="utf-8")
            description = json.loads(run_report_from(case, draft_a, describe=True, report=report).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            case.env["OMO_TEST_RENAME_AFTER_MESSAGE_SNAPSHOT"] = str(draft_b)

            continued = run_report_from(case, draft_a, report=report)

            case.env.pop("OMO_TEST_RENAME_AFTER_MESSAGE_SNAPSHOT")
            self.assertEqual(0, continued.returncode, continued.stderr)
            self.assertFalse(json.loads(continued.stdout)["accepted"])
            commitment = json.loads(transaction_commitment_path(description).read_bytes())
            held_state = commitment["allocation"]["file_at_submission"]
            self.assertEqual(original_a.stat().st_ino, held_state["inode"])
            self.assertNotEqual(draft_a.stat().st_ino, held_state["inode"])
            envelope = Path(str(description["files"]["private_envelope"])).read_bytes()
            self.assertIn(body, envelope)
            self.assertNotIn(replacement_body, envelope)
            manager_pending = case.manager.read_bytes()

            rejected_replacement = run_report_from(case, draft_a, report=report)

            self.assertEqual(2, rejected_replacement.returncode)
            self.assertIn("bound to a different allocation", rejected_replacement.stderr)
            self.assertEqual(manager_pending, case.manager.read_bytes())
            self.assertEqual(1, len(list(transaction_commitment_path(description).parent.glob("*.commitment"))))

    def test_concurrent_distinct_manager_routes_serialize_same_draft_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first_tmp = tmp_path / "first"
            second_tmp = tmp_path / "second"
            first_tmp.mkdir()
            second_tmp.mkdir()
            first = fixture(first_tmp, body=b"unused\n")
            second = fixture(second_tmp, body=b"unused\n")
            draft = allocate_report_draft(first, b"shared draft across manager routes\n")
            self.addCleanup(draft.unlink, missing_ok=True)
            aliased_draft = draft.parent / ".." / draft.parent.name / draft.name
            self.assertNotEqual(str(draft), str(aliased_draft))
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = (
                "        reject_pending_allocation_rebind(plan)\n"
                "        require_absent(plan.transaction_commitment_temporary, \"transaction commitment temporary file\")\n"
            )
            injection = (
                "        reject_pending_allocation_rebind(plan)\n"
                '        ready = os.environ.get("OMO_TEST_ALLOCATION_LOCK_READY")\n'
                '        release = os.environ.get("OMO_TEST_ALLOCATION_LOCK_RELEASE")\n'
                "        if ready and release:\n"
                "            with open(ready, \"ab\", buffering=0) as file:\n"
                "                file.write(b\"1\\n\")\n"
                "                os.fsync(file.fileno())\n"
                "            deadline = time.monotonic() + 5\n"
                "            while not Path(release).exists():\n"
                "                if time.monotonic() >= deadline:\n"
                "                    raise ReceiptError(\"allocation lock test timed out\")\n"
                "                time.sleep(0.01)\n"
                "        require_absent(plan.transaction_commitment_temporary, \"transaction commitment temporary file\")\n"
            )
            self.assertIn(marker, source)
            receiver.write_text(source.replace(marker, injection, 1), encoding="utf-8")
            shared_state = tmp_path / "shared-state"
            ready = tmp_path / "ready"
            release = tmp_path / "release"
            common_env = {
                "OMO_REPORT_ACK_TIMEOUT_S": "0",
                "OMO_TEST_ALLOCATION_LOCK_READY": str(ready),
                "OMO_TEST_ALLOCATION_LOCK_RELEASE": str(release),
                "XDG_STATE_HOME": str(shared_state),
            }
            first_command = replace(first, message=draft).command()
            second_command = replace(second, message=aliased_draft).command()
            first_command[0] = str(report)
            second_command[0] = str(report)
            first_process = subprocess.Popen(
                first_command,
                cwd=first.root.parent,
                env={**first.env, **common_env},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("first manager route did not reach the allocation lock")
                time.sleep(0.01)
            second_process = subprocess.Popen(
                second_command,
                cwd=second.root.parent,
                env={**second.env, **common_env},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.2)
                self.assertEqual("1\n", ready.read_text(encoding="utf-8"))
            finally:
                release.write_text("release\n", encoding="utf-8")
            first_output = first_process.communicate(timeout=10)
            second_output = second_process.communicate(timeout=10)
            results = ((first_process, first_output), (second_process, second_output))
            self.assertEqual([0, 2], sorted(process.returncode for process, _ in results))
            rejected_stderr = next(stderr for process, (_, stderr) in results if process.returncode == 2)
            self.assertIn("bound to a different allocation", rejected_stderr)
            commitment_directory = shared_state / "omo-manager" / "report-receipts"
            self.assertEqual(1, len(list(commitment_directory.glob("*.commitment"))))
            self.assertEqual(1, sum(manager.exists() for manager in (first.manager, second.manager)))

    def test_concurrent_distinct_drafts_claim_one_pending_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            body = b"concurrent A/B immutable body\n"
            drafts = [allocate_report_draft(case, body) for _ in range(2)]
            for draft in drafts:
                self.addCleanup(draft.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, drafts[0], describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            processes = [
                subprocess.Popen(
                    replace(case, message=draft).command(),
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for draft in drafts
            ]
            results = [process.communicate(timeout=20) for process in processes]
            pending_indices = [index for index, (stdout, _) in enumerate(results) if processes[index].returncode == 0 and not json.loads(stdout)["accepted"]]
            rejected_indices = [index for index, (_, stderr) in enumerate(results) if processes[index].returncode == 2 and "different allocation" in stderr]
            self.assertEqual(1, len(pending_indices), results)
            self.assertEqual(1, len(rejected_indices), results)
            winner = drafts[pending_indices[0]]
            loser = drafts[rejected_indices[0]]
            commitment = json.loads(transaction_commitment_path(description).read_bytes())
            self.assertEqual(str(winner), commitment["preflight"]["allocation"]["file"])
            self.assertNotIn(str(loser), json.dumps(commitment, sort_keys=True))
            self.assertEqual(1, case.manager.read_text(encoding="utf-8").count("(pending)"))

            watched = run_manager_watcher_once(case)
            self.assertEqual(0, watched.returncode, watched.stderr)
            accepted = run_report_from(case, winner)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            replay = run_report_from(case, loser)
            self.assertEqual(accepted.stdout, replay.stdout)
            receipt = private_receipt(json.loads(accepted.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual(str(winner), receipt["preflight"]["allocation"]["file"])
            self.assertEqual(2, len(list(Path(str(description["files"]["private_receipt"])).parent.glob("*.json"))))

    def test_stale_commitment_without_pointer_allows_only_original_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, body=b"unused\n")
            body = b"stale commitment body\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            self.addCleanup(draft_b.unlink, missing_ok=True)
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = "                commitment = create_or_reuse_transaction_commitment(plan)\n"
            self.assertIn(marker, source)
            receiver.write_text(
                source.replace(
                    marker,
                    marker
                    + '                if os.environ.get("OMO_TEST_FAIL_AFTER_COMMITMENT") == "1":\n'
                    + '                    raise ReceiptError("injected post-commitment failure")\n',
                    1,
                ),
                encoding="utf-8",
            )
            description = json.loads(run_report_from(case, draft_a, describe=True, report=report).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            case.env["OMO_TEST_FAIL_AFTER_COMMITMENT"] = "1"

            failed_a = run_report_from(case, draft_a, report=report)

            case.env.pop("OMO_TEST_FAIL_AFTER_COMMITMENT")
            self.assertEqual(2, failed_a.returncode)
            self.assertIn("injected post-commitment failure", failed_a.stderr)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            self.assertFalse(case.manager.exists())
            self.assertFalse(Path(str(description["files"]["private_envelope"])).exists())
            rejected_b = run_report_from(case, draft_b, report=report)
            self.assertEqual(2, rejected_b.returncode)
            self.assertIn("bound to a different allocation", rejected_b.stderr)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertFalse(case.manager.exists())

            pending_a = run_report_from(case, draft_a, report=report)
            self.assertEqual(0, pending_a.returncode, pending_a.stderr)
            self.assertFalse(json.loads(pending_a.stdout)["accepted"])
            watched = run_manager_watcher_once(case)
            self.assertEqual(0, watched.returncode, watched.stderr)
            accepted_a = run_report_from(case, draft_a, report=report)
            self.assertEqual(0, accepted_a.returncode, accepted_a.stderr)
            self.assertTrue(json.loads(accepted_a.stdout)["accepted"])

    def test_rejected_pending_rebind_is_private_and_side_effect_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            body = b"rejected draft remains caller-owned and private\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            self.addCleanup(draft_b.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending_a = run_report_from(case, draft_a)
            self.assertEqual(0, pending_a.returncode, pending_a.stderr)
            files = description["files"]
            self.assertIsInstance(files, dict)
            commitment_path = transaction_commitment_path(description)
            observed_paths = [case.manager, Path(str(files["private_envelope"])), commitment_path]
            before = {path: path.read_bytes() for path in observed_paths}
            receipt_directory = Path(str(files["private_receipt"])).parent
            names_before = sorted(path.name for path in receipt_directory.iterdir())

            rejected_b = run_report_from(case, draft_b)

            self.assertEqual(2, rejected_b.returncode)
            self.assertEqual("", rejected_b.stdout)
            self.assertNotIn(body.decode().strip(), rejected_b.stderr)
            self.assertNotIn(str(draft_a), rejected_b.stderr)
            self.assertNotIn(str(draft_b), rejected_b.stderr)
            self.assertEqual(before, {path: path.read_bytes() for path in observed_paths})
            self.assertEqual(names_before, sorted(path.name for path in receipt_directory.iterdir()))
            self.assertEqual(body, draft_b.read_bytes())
            self.assertEqual(0o600, draft_b.stat().st_mode & 0o777)
            self.assertFalse(any(path.name.endswith(".tmp") for path in receipt_directory.iterdir()))

    def test_watcher_restart_keeps_first_commitment_and_single_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            owner = b"watcher restart owner\n"
            case.manager.write_bytes(owner)
            body = b"watcher restart A/B\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            self.addCleanup(draft_b.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending_a = run_report_from(case, draft_a)
            self.assertEqual(0, pending_a.returncode, pending_a.stderr)
            rejected_b = run_report_from(case, draft_b)
            self.assertEqual(2, rejected_b.returncode)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()

            first_watcher = run_manager_watcher_once(case)
            restarted_watcher = run_manager_watcher_once(case)

            self.assertEqual(0, first_watcher.returncode, first_watcher.stderr)
            self.assertNotEqual(0, restarted_watcher.returncode)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(owner, case.manager.read_bytes())
            accepted = run_report_from(case, draft_a)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            receipt = private_receipt(json.loads(accepted.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            receipt_path = Path(str(description["files"]["private_receipt"]))
            self.assertEqual(2, len(list(receipt_path.parent.glob("*.json"))))

    def test_post_acceptance_distinct_draft_replays_first_commitment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"unused\n")
            body = b"accepted stable draft replay\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            self.addCleanup(draft_b.unlink, missing_ok=True)
            case_a = replace(case, message=draft_a)
            description_a, accepted_a = run_accepted(case_a)
            self.assertEqual(0, accepted_a.returncode, accepted_a.stderr)

            description_b = run_report_from(case, draft_b, describe=True)
            accepted_b = run_report_from(case, draft_b)

            self.assertEqual(0, description_b.returncode, description_b.stderr)
            self.assertEqual(accepted_a.stdout, accepted_b.stdout)
            replay_description = json.loads(description_b.stdout)
            self.assertEqual(description_a["transaction"], replay_description["transaction"])
            commitment = json.loads(transaction_commitment_path(description_a).read_bytes())
            self.assertEqual(str(draft_a), commitment["preflight"]["allocation"]["file"])
            self.assertNotIn(str(draft_b), json.dumps(commitment, sort_keys=True))
            for public in (description_b.stdout, accepted_b.stdout):
                self.assertNotIn(body.decode().strip(), public)
                self.assertNotIn(str(draft_a), public)
                self.assertNotIn(str(draft_b), public)

    def test_commitment_cleanup_failure_fails_closed_and_recovers_for_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, body=b"unused\n")
            owner = b"cleanup failure owner"
            case.manager.write_bytes(owner)
            body = b"commitment cleanup A/B\n"
            draft_a = allocate_report_draft(case, body)
            draft_b = allocate_report_draft(case, body)
            original_a = draft_a.with_name(f".{draft_a.name}.original")
            for draft in (draft_a, draft_b, original_a):
                self.addCleanup(draft.unlink, missing_ok=True)
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = (
                "        try:\n"
                "            plan.transaction_commitment_temporary.unlink()\n"
                "        except OSError as exc:\n"
                '            raise ReceiptError("cannot retire transaction commitment temporary name") from exc\n'
                "        fsync_directory(plan.receipt_directory)\n"
            )
            offset = source.rfind(marker)
            self.assertGreaterEqual(offset, 0)
            injection = (
                '        if os.environ.get("OMO_TEST_FAIL_COMMITMENT_CLEANUP") == "1":\n'
                '            raise ReceiptError("injected commitment cleanup failure")\n'
                + marker
            )
            receiver.write_text(source[:offset] + injection + source[offset + len(marker) :], encoding="utf-8")
            description = json.loads(run_report_from(case, draft_a, describe=True, report=report).stdout)
            commitment_path = transaction_commitment_path(description)
            commitment_temporary = commitment_path.with_name(f".{commitment_path.name}.tmp")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            case.env["OMO_TEST_FAIL_COMMITMENT_CLEANUP"] = "1"

            failed_a = run_report_from(case, draft_a, report=report)

            case.env.pop("OMO_TEST_FAIL_COMMITMENT_CLEANUP")
            self.assertEqual(2, failed_a.returncode)
            self.assertIn("injected commitment cleanup failure", failed_a.stderr)
            self.assertTrue(commitment_path.exists())
            self.assertTrue(commitment_temporary.exists())
            self.assertEqual(commitment_path.stat().st_ino, commitment_temporary.stat().st_ino)
            self.assertEqual(owner, case.manager.read_bytes())
            rejected_b = run_report_from(case, draft_b, report=report)
            self.assertEqual(2, rejected_b.returncode)
            self.assertIn("bound to a different allocation", rejected_b.stderr)
            self.assertTrue(commitment_temporary.exists())
            self.assertEqual(owner, case.manager.read_bytes())
            os.link(draft_a, original_a)
            os.replace(draft_b, draft_a)
            rejected_replacement = run_report_from(case, draft_a, report=report)
            self.assertEqual(2, rejected_replacement.returncode)
            self.assertIn("draft object identity differs", rejected_replacement.stderr)
            self.assertTrue(commitment_temporary.exists())
            self.assertEqual(owner, case.manager.read_bytes())

            os.replace(original_a, draft_a)
            pending_a = run_report_from(case, draft_a, report=report)
            self.assertEqual(0, pending_a.returncode, pending_a.stderr)
            self.assertFalse(json.loads(pending_a.stdout)["accepted"])
            self.assertFalse(commitment_temporary.exists())
            watched = run_manager_watcher_once(case)
            self.assertEqual(0, watched.returncode, watched.stderr)
            accepted_a = run_report_from(case, draft_a, report=report)
            self.assertEqual(0, accepted_a.returncode, accepted_a.stderr)
            receipt = private_receipt(json.loads(accepted_a.stdout))
            self.assertTrue(all(not Path(path).exists() for path in receipt["preflight"]["temporary_files"]))
            self.assertEqual(owner, case.manager.read_bytes())

    def test_identical_bytes_at_unrelated_draft_paths_do_not_rebind_pending_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, body=b"same bytes outside allocator\n")
            private_directory = tmp_path / "other-private"
            private_directory.mkdir(mode=0o700)
            draft_b = private_directory / "different-name.md"
            draft_b.write_bytes(case.message.read_bytes())
            draft_b.chmod(0o600)
            description_a = json.loads(run_report(case, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending_a = run_report(case)
            self.assertEqual(0, pending_a.returncode, pending_a.stderr)

            rejected_b = run_report_from(case, draft_b)

            self.assertEqual(2, rejected_b.returncode)
            self.assertIn("bound to a different allocation", rejected_b.stderr)
            commitment = json.loads(transaction_commitment_path(description_a).read_bytes())
            self.assertEqual(str(case.message), commitment["preflight"]["allocation"]["file"])
            self.assertNotIn(str(draft_b), json.dumps(commitment, sort_keys=True))
            watched = run_manager_watcher_once(case)
            self.assertEqual(0, watched.returncode, watched.stderr)
            accepted_a = run_report(case)
            self.assertEqual(0, accepted_a.returncode, accepted_a.stderr)
            accepted_b = run_report_from(case, draft_b)
            self.assertEqual(accepted_a.stdout, accepted_b.stdout)

    def test_active_manager_same_draft_immediate_retry_reuses_first_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case, manager, owner = active_manager_fixture(Path(tmp))
            body = b"active manager same-A immediate retry stays private\n"
            draft_a = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            described = run_report_from(case, draft_a, describe=True)
            self.assertEqual(0, described.returncode, described.stderr)
            description = json.loads(described.stdout)
            self.assertEqual(str(manager), description["files"]["manager"])
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"

            pending = run_report_from(case, draft_a)

            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            commitment = json.loads(commitment_bytes)
            frozen_preflight = commitment["preflight"]
            frozen_sources = frozen_preflight["routing_sources"]
            frozen_manager = next(item for item in frozen_sources if item["path"] == str(manager))
            self.assertEqual(hashlib.sha256(owner).hexdigest(), frozen_manager["sha256"])
            self.assertEqual(len(owner), frozen_manager["size_bytes"])
            envelope = Path(str(description["files"]["private_envelope"]))
            pointer = f"(from agent cfg:7 {envelope})"
            manager_pending = owner + b"\n(pending)\n" + pointer.encode() + b"\n"
            self.assertEqual(manager_pending, manager.read_bytes())
            receipt_directory = commitment_path.parent
            names_after_first = sorted(path.name for path in receipt_directory.iterdir())
            envelope_bytes_after_first = envelope.read_bytes()

            retry_description = run_report_from(case, draft_a, describe=True)
            retry = run_report_from(case, draft_a)

            self.assertEqual(0, retry_description.returncode, retry_description.stderr)
            self.assertEqual(description["transaction"], json.loads(retry_description.stdout)["transaction"])
            self.assertEqual(0, retry.returncode, retry.stderr)
            self.assertEqual(pending.stdout, retry.stdout)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(envelope_bytes_after_first, envelope.read_bytes())
            self.assertEqual(manager_pending, manager.read_bytes())
            self.assertEqual(names_after_first, sorted(path.name for path in receipt_directory.iterdir()))
            self.assertEqual(1, manager.read_text(encoding="utf-8").count(pointer))

            watched = run_manager_watcher_once(case, manager)
            self.assertEqual(0, watched.returncode, watched.stderr)
            self.assertEqual(owner, manager.read_bytes())
            accepted = run_report_from(case, draft_a)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            acceptance = json.loads(accepted.stdout)
            receipt = private_receipt(acceptance)
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual(frozen_preflight, receipt["preflight"])
            self.assertEqual(frozen_sources, receipt["routing"]["route_evidence"])
            self.assertEqual(owner, manager.read_bytes())
            self.assertEqual(1, len(list(receipt_directory.glob("*.commitment"))))
            self.assertEqual(2, len(list(receipt_directory.glob("*.json"))))
            self.assertTrue(all(not Path(path).exists() for path in receipt["preflight"]["temporary_files"]))
            acknowledgment_state, acknowledgment_key = acknowledgment_coordinates(description)
            self.assertEqual(1, acknowledgment_state.read_text(encoding="utf-8").count(acknowledgment_key))
            for public in (described.stdout, retry_description.stdout, pending.stdout, retry.stdout, accepted.stdout):
                self.assertNotIn(body.decode().strip(), public)
                self.assertNotIn(str(draft_a), public)

    def test_consumed_closure_attests_exact_historical_transition_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case, manager, owner = active_manager_fixture(tmp_path)
            body = f"historically consumed report closure {tmp}\n".encode()
            draft = allocate_report_draft(case, body)
            self.addCleanup(draft.unlink, missing_ok=True)
            report = copy_report_helper(tmp_path)
            described = run_report_from(case, draft, describe=True, status="progressing", report=report)
            self.assertEqual(0, described.returncode, described.stderr)
            description = json.loads(described.stdout)
            premature = run_report_from(case, draft, verify_consumed=True, status="progressing", report=report)
            self.assertNotEqual(0, premature.returncode)
            self.assertEqual(owner, manager.read_bytes())
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft, status="progressing", report=report)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])

            watched = run_manager_watcher_once(case, manager)
            self.assertEqual(0, watched.returncode, watched.stderr)
            self.assertEqual(owner, manager.read_bytes())
            manager.write_bytes(owner + b"\nnew manager-owned work after consumption\n")
            (case.root / "TODO.md").write_text(
                "current:\nworker.md cfg:7\nmanager.md vl:2\nnew-worker.md cfg:8\n",
                encoding="utf-8",
            )
            manager_after = manager.read_bytes()
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            malformed = json.loads(commitment_bytes)
            malformed["preflight"]["routing_sources"] = [{}]
            unsigned_commitment = {key: value for key, value in malformed.items() if key != "commitment_id"}
            malformed["commitment_id"] = hashlib.sha256(
                json.dumps(
                    unsigned_commitment,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            commitment_path.write_bytes(
                (json.dumps(malformed, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
            )
            rejected = run_report_from(case, draft, verify_consumed=True, status="progressing", report=report)
            self.assertNotEqual(0, rejected.returncode)
            self.assertEqual(manager_after, manager.read_bytes())
            self.assertFalse(Path(str(description["files"]["private_receipt"])).exists())
            self.assertFalse(Path(str(description["files"]["receipt_publication"])).exists())
            commitment_path.write_bytes(commitment_bytes)
            with (report.parent / "omo_report_receipt.py").open("a", encoding="utf-8") as stream:
                stream.write("\n# upgraded after historical watcher consumption\n")
            with report.open("a", encoding="utf-8") as stream:
                stream.write("\n# upgraded after historical watcher consumption\n")

            verified = run_report_from(case, draft, verify_consumed=True, status="progressing", report=report)

            self.assertEqual(0, verified.returncode, verified.stderr)
            attestation = json.loads(verified.stdout)
            self.assertEqual("omo-report-consumed-closure/v1", attestation["schema"])
            self.assertFalse(attestation["accepted"])
            self.assertTrue(attestation["terminal"])
            self.assertEqual("in-progress", attestation["status"])
            self.assertEqual(description["input"], attestation["input"])
            self.assertEqual(description["receipt"]["replay_id"], attestation["replay_id"])
            unsigned = {key: value for key, value in attestation.items() if key != "attestation_id"}
            self.assertEqual(
                hashlib.sha256(
                    json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                attestation["attestation_id"],
            )
            files = description["files"]
            self.assertFalse(Path(str(files["private_receipt"])).exists())
            self.assertFalse(Path(str(files["receipt_publication"])).exists())
            self.assertEqual(manager_after, manager.read_bytes())
            acknowledgment_state, acknowledgment_key = acknowledgment_coordinates(description)
            self.assertEqual(1, acknowledgment_state.read_text(encoding="utf-8").count(acknowledgment_key))
            self.assertNotIn(body.decode().strip(), verified.stdout + verified.stderr)
            self.assertNotIn(str(draft), verified.stdout + verified.stderr)

    def test_active_manager_concurrent_same_draft_retries_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case, manager, owner = active_manager_fixture(Path(tmp))
            body = b"active manager concurrent same-A retry\n"
            draft_a = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft_a)
            self.assertEqual(0, pending.returncode, pending.stderr)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()
            manager_pending = manager.read_bytes()
            receipt_directory = commitment_path.parent
            names_after_first = sorted(path.name for path in receipt_directory.iterdir())
            processes = [
                subprocess.Popen(
                    replace(case, message=draft_a).command(),
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]

            results = [process.communicate(timeout=20) for process in processes]

            for process, (stdout, stderr) in zip(processes, results, strict=True):
                self.assertEqual(0, process.returncode, stderr)
                self.assertEqual(pending.stdout, stdout)
                self.assertNotIn(body.decode().strip(), stdout + stderr)
                self.assertNotIn(str(draft_a), stdout + stderr)
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            self.assertEqual(manager_pending, manager.read_bytes())
            self.assertEqual(names_after_first, sorted(path.name for path in receipt_directory.iterdir()))
            self.assertEqual(1, manager.read_text(encoding="utf-8").count("(pending)"))
            watched = run_manager_watcher_once(case, manager)
            self.assertEqual(0, watched.returncode, watched.stderr)
            accepted = run_report_from(case, draft_a)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            receipt = private_receipt(json.loads(accepted.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual(json.loads(commitment_bytes)["preflight"], receipt["preflight"])
            self.assertEqual(owner, manager.read_bytes())
            self.assertEqual(2, len(list(receipt_directory.glob("*.json"))))

    def test_active_manager_same_draft_retry_survives_watcher_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case, manager, owner = active_manager_fixture(Path(tmp))
            body = b"active manager same-A watcher restart\n"
            draft_a = allocate_report_draft(case, body)
            self.addCleanup(draft_a.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft_a)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertEqual(pending.stdout, run_report_from(case, draft_a).stdout)
            commitment_path = transaction_commitment_path(description)
            commitment_bytes = commitment_path.read_bytes()

            first_watcher = run_manager_watcher_once(case, manager)
            restarted_watcher = run_manager_watcher_once(case, manager)

            self.assertEqual(0, first_watcher.returncode, first_watcher.stderr)
            self.assertNotEqual(0, restarted_watcher.returncode)
            self.assertEqual(owner, manager.read_bytes())
            self.assertEqual(commitment_bytes, commitment_path.read_bytes())
            accepted = run_report_from(case, draft_a)
            replay = run_report_from(case, draft_a)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(accepted.stdout, replay.stdout)
            receipt = private_receipt(json.loads(accepted.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual(json.loads(commitment_bytes)["preflight"], receipt["preflight"])
            self.assertEqual(2, len(list(commitment_path.parent.glob("*.json"))))
            acknowledgment_state, acknowledgment_key = acknowledgment_coordinates(description)
            self.assertEqual(1, acknowledgment_state.read_text(encoding="utf-8").count(acknowledgment_key))
            for public in (pending.stdout, accepted.stdout, replay.stdout):
                self.assertNotIn(body.decode().strip(), public)
                self.assertNotIn(str(draft_a), public)

    def test_active_manager_same_a_and_distinct_drafts_interleave_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case, manager, owner = active_manager_fixture(Path(tmp))
            body = b"active manager A/B/C interleaving\n"
            draft_a, draft_b, draft_c = (allocate_report_draft(case, body) for _ in range(3))
            for draft in (draft_a, draft_b, draft_c):
                self.addCleanup(draft.unlink, missing_ok=True)
            description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report_from(case, draft_a)
            self.assertEqual(0, pending.returncode, pending.stderr)
            commitment_path = transaction_commitment_path(description)
            immutable_paths = {
                commitment_path: commitment_path.read_bytes(),
                manager: manager.read_bytes(),
                Path(str(description["files"]["private_envelope"])): Path(str(description["files"]["private_envelope"])).read_bytes(),
            }
            receipt_directory = commitment_path.parent
            names_after_first = sorted(path.name for path in receipt_directory.iterdir())
            interleaved = (draft_a, draft_b, draft_a, draft_c)
            processes = [
                subprocess.Popen(
                    replace(case, message=draft).command(),
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for draft in interleaved
            ]

            results = [process.communicate(timeout=20) for process in processes]

            for draft, process, (stdout, stderr) in zip(interleaved, processes, results, strict=True):
                if draft == draft_a:
                    self.assertEqual(0, process.returncode, stderr)
                    self.assertEqual(pending.stdout, stdout)
                else:
                    self.assertEqual(2, process.returncode)
                    self.assertEqual("", stdout)
                    self.assertIn("bound to a different allocation", stderr)
                self.assertNotIn(body.decode().strip(), stdout + stderr)
                self.assertNotIn(str(draft_a), stdout + stderr)
                self.assertNotIn(str(draft_b), stdout + stderr)
                self.assertNotIn(str(draft_c), stdout + stderr)
            self.assertEqual(immutable_paths, {path: path.read_bytes() for path in immutable_paths})
            self.assertEqual(names_after_first, sorted(path.name for path in receipt_directory.iterdir()))
            self.assertEqual(body, draft_b.read_bytes())
            self.assertEqual(body, draft_c.read_bytes())
            self.assertEqual(1, manager.read_text(encoding="utf-8").count("(pending)"))
            watched = run_manager_watcher_once(case, manager)
            self.assertEqual(0, watched.returncode, watched.stderr)
            accepted_a = run_report_from(case, draft_a)
            self.assertEqual(0, accepted_a.returncode, accepted_a.stderr)
            self.assertEqual(accepted_a.stdout, run_report_from(case, draft_b).stdout)
            self.assertEqual(accepted_a.stdout, run_report_from(case, draft_c).stdout)
            receipt = private_receipt(json.loads(accepted_a.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual(str(draft_a), receipt["preflight"]["allocation"]["file"])
            self.assertNotIn(str(draft_b), json.dumps(receipt, sort_keys=True))
            self.assertNotIn(str(draft_c), json.dumps(receipt, sort_keys=True))
            self.assertEqual(owner, manager.read_bytes())
            self.assertEqual(2, len(list(receipt_directory.glob("*.json"))))
            self.assertTrue(all(not Path(path).exists() for path in receipt["preflight"]["temporary_files"]))

    def test_integer_exists_values_in_committed_route_evidence_are_malformed(self) -> None:
        for malformed_exists in (0, 1):
            with self.subTest(exists=malformed_exists), tempfile.TemporaryDirectory() as tmp:
                case, manager, _owner = active_manager_fixture(Path(tmp))
                body = b"integer route-evidence boolean stays private\n"
                draft_a = allocate_report_draft(case, body)
                self.addCleanup(draft_a.unlink, missing_ok=True)
                description = json.loads(run_report_from(case, draft_a, describe=True).stdout)
                case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
                pending = run_report_from(case, draft_a)
                self.assertEqual(0, pending.returncode, pending.stderr)
                commitment_path = transaction_commitment_path(description)
                commitment = json.loads(commitment_path.read_text(encoding="utf-8"))
                preflight = commitment["preflight"]
                source = next(item for item in preflight["routing_sources"] if item["path"] != str(manager))
                source["exists"] = malformed_exists
                if malformed_exists == 0:
                    source.pop("sha256")
                    source.pop("size_bytes")
                preflight_without_sha = dict(preflight)
                preflight_without_sha.pop("sha256")
                preflight["sha256"] = hashlib.sha256(
                    json.dumps(preflight_without_sha, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                commitment_without_id = dict(commitment)
                commitment_without_id.pop("commitment_id")
                commitment["commitment_id"] = hashlib.sha256(
                    json.dumps(commitment_without_id, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                commitment_path.write_text(
                    json.dumps(commitment, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                commitment_path.chmod(0o600)
                manager_pending = manager.read_bytes()

                rejected = run_report_from(case, draft_a)

                self.assertEqual(2, rejected.returncode)
                self.assertEqual("", rejected.stdout)
                self.assertIn("transaction commitment route evidence is malformed", rejected.stderr)
                self.assertNotIn(body.decode().strip(), rejected.stderr)
                self.assertNotIn(str(draft_a), rejected.stderr)
                self.assertEqual(manager_pending, manager.read_bytes())
                self.assertFalse(Path(str(description["files"]["private_receipt"])).exists())
                self.assertFalse(Path(str(description["files"]["receipt_publication"])).exists())

    def test_describe_is_deterministic_read_only_minimal_and_operation_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"describe-only secret\n")
            before = {path.relative_to(case.root): path.read_bytes() for path in case.root.rglob("*") if path.is_file()}

            first = run_report(case, describe=True, status=" IN_PROGRESS ")
            second = run_report(case, describe=True, status=" IN_PROGRESS ")

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual("", first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            description = json.loads(first.stdout)
            self.assertEqual("omo-report-description/v1", description["schema"])
            self.assertTrue(description["read_only"])
            self.assertEqual("in-progress", description["status"])
            self.assertTrue(description["receipt"]["support_available"])
            self.assertFalse(description["receipt"]["migration_bindings_available"])
            self.assertEqual(hashlib.sha256(case.message.read_bytes()).hexdigest(), description["input"]["sha256"])
            self.assertEqual(case.message.stat().st_size, description["input"]["size_bytes"])
            self.assertEqual(str(case.manager), description["routing"]["manager"])
            self.assertEqual(str(case.manager), description["files"]["manager"])
            self.assertLess(len(first.stdout.encode()), 4096)
            self.assertNotIn("route_evidence", first.stdout)
            self.assertNotIn("side_effects", first.stdout)
            self.assertNotIn('"helper"', first.stdout)
            self.assertIn("exclusive-create-0600-draft", description["operations"]["allocation"])
            self.assertIn("atomic-replace", description["operations"]["manager_file"])
            self.assertIn("external-manager-watcher", description["operations"]["manager_acknowledgment"])
            self.assertEqual(
                {"manager_file", "report_transaction"},
                set(description["locks"]),
            )
            transaction = description["transaction"]
            self.assertEqual("omo-report-preflight-binding/v1", transaction["schema"])
            self.assertRegex(transaction["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(transaction["allocation_file_path_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreaterEqual(transaction["path_count"], 20)
            self.assertEqual(2, transaction["route_source_count"])
            self.assertNotIn("describe-only secret", first.stdout)
            self.assertNotIn(str(case.message), first.stdout)
            after = {path.relative_to(case.root): path.read_bytes() for path in case.root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertFalse(case.manager.exists())
            self.assertFalse(Path(f"{case.manager}.omo_report.lock").exists())
            self.assertFalse((Path(tmp) / "state").exists())

    def test_describe_from_fresh_helper_copy_creates_no_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path)
            report = copy_report_helper(tmp_path)
            command = case.command(describe=True)
            command[0] = str(report)

            result = subprocess.run(
                command,
                cwd=case.root.parent,
                env=case.env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], list(report.parent.rglob("*.pyc")))
            self.assertFalse((report.parent / "__pycache__").exists())

    def test_submission_returns_body_free_durable_acceptance_receipt(self) -> None:
        secret = "receipt must not disclose this body"
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=f"batch: B01\nattempt: attempt-0002\n{secret}\n".encode())
            described, result = run_accepted(case)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            acceptance = json.loads(result.stdout)
            receipt = private_receipt(acceptance)
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual("omo-report-acceptance/v1", acceptance["schema"])
            self.assertEqual("omo-report-receipt/v1", receipt["schema"])
            self.assertTrue(acceptance["accepted"])
            self.assertTrue(acceptance["manager_acknowledged"])
            self.assertRegex(acceptance["accepted_at_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")
            self.assertEqual("done", acceptance["status"])
            self.assertIsNone(described["receipt"]["receipt_id"])
            self.assertEqual(described["receipt"]["replay_id"], acceptance["replay_id"])
            self.assertRegex(acceptance["receipt_id"], r"^[0-9a-f]{64}$")
            self.assertTrue(described["receipt"]["migration_bindings_available"])
            self.assertEqual({"attempt": "attempt-0002", "batch": "B01"}, receipt["report_context"])
            self.assertEqual(str(case.root / "worker.md"), receipt["routing"]["task"])
            self.assertEqual(str(case.manager), receipt["routing"]["manager"])
            self.assertEqual("cfg:7", receipt["routing"]["producer_target"])
            self.assertEqual(hashlib.sha256(case.message.read_bytes()).hexdigest(), receipt["input"]["sha256"])
            self.assertEqual(
                {"omo_pending_digest", "omo_task_lock"},
                set(receipt["helper"]["dependencies"]),
            )
            self.assertEqual(
                "immutable-pipe-and-memory-compiled-sources",
                receipt["helper"]["execution"],
            )
            self.assertLess(len(result.stdout.encode()), 4096)
            self.assertNotIn("route_evidence", result.stdout)
            self.assertNotIn("side_effects", result.stdout)
            self.assertNotIn('"helper"', result.stdout)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(str(case.message), result.stdout)
            manager_text = case.manager.read_text(encoding="utf-8")
            self.assertNotIn(secret, manager_text)
            self.assertNotIn("(pending)", manager_text)
            envelope = Path(str(described["files"]["private_envelope"]))
            self.assertEqual(0o600, envelope.stat().st_mode & 0o777)
            self.assertIn(secret, envelope.read_text(encoding="utf-8"))
            record = Path(str(acceptance["receipt_path"]))
            self.assertTrue(record.is_file())
            self.assertEqual(0o600, record.stat().st_mode & 0o777)
            self.assertNotIn(secret, record.read_text(encoding="utf-8"))
            publication = Path(str(acceptance["publication_path"]))
            self.assertEqual(0o600, publication.stat().st_mode & 0o777)
            publication_record = json.loads(publication.read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(record.read_bytes()).hexdigest(), publication_record["receipt_state"]["sha256"])
            self.assertEqual(record.stat().st_size, publication_record["receipt_state"]["size"])
            self.assertEqual(publication.stat().st_size, acceptance["publication_state"]["size"])
            effects = receipt["side_effects"]
            preflight = receipt["preflight"]
            declared_paths = preflight_paths(preflight)
            self.assertEqual(described["transaction"]["sha256"], preflight["sha256"])
            self.assertEqual(described["transaction"]["path_count"], len(declared_paths))
            self.assertIn(str(case.message), declared_paths)
            self.assertNotIn(secret, json.dumps(preflight, sort_keys=True))
            self.assertLessEqual(side_effect_paths(effects), declared_paths)
            self.assertTrue(all(not Path(path).exists() for path in preflight["temporary_files"]))
            self.assertIn("before", effects["manager_file"])
            self.assertIn("after", effects["manager_file"])
            self.assertIn("before", effects["private_envelope"])
            self.assertIn("after", effects["private_envelope"])
            self.assertIn("before", effects["locks"]["adjacent_report"])
            self.assertIn("after", effects["locks"]["adjacent_report"])
            allocation_replay = effects["locks"]["allocation_replay"]
            self.assertEqual(preflight["locks"]["allocation_replay"], allocation_replay["path"])
            self.assertEqual("create-or-open-and-flock-exclusive", allocation_replay["operation"])
            self.assertEqual(0o600, Path(str(allocation_replay["path"])).stat().st_mode & 0o777)
            self.assertEqual({"exists": False}, allocation_replay["before"])
            self.assertTrue(allocation_replay["after"]["exists"])
            self.assertIn("before", effects["locks"]["task_file"])
            self.assertIn("after", effects["locks"]["task_file"])
            for lock in effects["locks"]["route_evidence"]:
                self.assertIn("before", lock)
                self.assertIn("after", lock)
            sources = {item["source"] for item in effects["locks"]["route_evidence"]}
            self.assertEqual({str(case.root / "TODO.md"), str(case.root / "worker.md")}, sources)
            self.assertEqual("omo-pending-watch-consumed-report/v1", effects["manager_acknowledgment"]["schema"])
            self.assertEqual("external-manager-watcher-atomic-replace-consumed-record", effects["manager_acknowledgment"]["operation"])
            self.assertEqual(
                "watcher-locked-pointer-transition-v1",
                effects["manager_acknowledgment"]["transition"]["protocol"],
            )
            self.assertEqual("external-manager-acknowledged-no-active-pointer", effects["manager_file"]["operation"])
            self.assertEqual("completed-before-submit", effects["private_allocation"]["operation"])
            self.assertEqual("write-fsync-rename-fsync-directory", effects["receipt_publication"]["operation"])

    def test_progressing_alias_has_canonical_status_and_replay_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"legacy progressing report\n")
            progressing = json.loads(run_report(case, describe=True, status="progressing").stdout)
            canonical = json.loads(run_report(case, describe=True, status="in-progress").stdout)
            self.assertEqual("in-progress", progressing["status"])
            self.assertEqual(canonical["receipt"]["replay_id"], progressing["receipt"]["replay_id"])

            _, result = run_accepted(case, status="progressing")
            self.assertEqual(0, result.returncode, result.stderr)
            acceptance = json.loads(result.stdout)
            receipt = private_receipt(acceptance)
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertTrue(acceptance["accepted"])
            self.assertEqual("in-progress", acceptance["status"])
            self.assertEqual(result.stdout, run_report(case, status="in-progress").stdout)

    def test_acceptance_requires_external_manager_acknowledgment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"manager must acknowledge\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            description = json.loads(run_report(case, describe=True).stdout)

            pending = run_report(case)

            self.assertEqual(0, pending.returncode, pending.stderr)
            pending_output = json.loads(pending.stdout)
            self.assertFalse(pending_output["accepted"])
            self.assertEqual("routed; manager acknowledgment pending", pending_output["reason"])
            self.assertTrue(pending_output["retry_required"])
            self.assertFalse(Path(description["files"]["private_receipt"]).exists())
            self.assertIn("(pending)", case.manager.read_text(encoding="utf-8"))

            manager_run = run_manager_watcher_once(case)
            self.assertEqual(0, manager_run.returncode, manager_run.stderr)
            self.assertNotIn("(pending)", case.manager.read_text(encoding="utf-8"))
            accepted = run_report(case)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            acceptance = json.loads(accepted.stdout)
            self.assertTrue(acceptance["accepted"])
            self.assertEqual("manager acknowledged routed report", acceptance["reason"])
            self.assertFalse(acceptance["retry_required"])
            self.assertNotIn("(pending)", case.manager.read_text(encoding="utf-8"))
            receipt = private_receipt(acceptance)
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertEqual(
                acknowledgment_coordinates(description)[1],
                receipt["side_effects"]["manager_acknowledgment"]["key"],
            )
            manager_effect = receipt["side_effects"]["manager_file"]
            self.assertEqual("external-manager-acknowledged-no-active-pointer", manager_effect["operation"])
            self.assertEqual(manager_effect["before"], manager_effect["after"])

    def test_exact_publication_releases_watcher_authority_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"release watcher authority after publication\n")
            description, accepted = run_accepted(case)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            receipt = private_receipt(json.loads(accepted.stdout))
            authority = receipt["side_effects"]["manager_acknowledgment"]["transition"]["authority"]
            pid = authority["pid"]
            start_ticks = authority["process_start_ticks"]
            state, key = acknowledgment_coordinates(description)
            lock = state.parent / "pending-watch-authority" / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
            completion = lock.with_name(f"{lock.name}.complete")
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                try:
                    process_stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                    current_start = int(process_stat[process_stat.rfind(") ") + 2 :].split()[19])
                except (FileNotFoundError, ProcessLookupError):
                    break
                if current_start != start_ticks:
                    break
                time.sleep(0.05)

            self.assertFalse(lock.exists())
            self.assertFalse(completion.exists())

    def test_prewritten_bare_ledger_line_cannot_acknowledge_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"not yet published\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            description = json.loads(run_report(case, describe=True).stdout)
            state, key = acknowledgment_coordinates(description)
            state.parent.mkdir(mode=0o700, parents=True)
            state.write_text(f"{time.time():.6f}\t{key}\n", encoding="utf-8")
            state.chmod(0o600)

            result = run_report(case)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(json.loads(result.stdout)["accepted"])
            self.assertIn("(pending)", case.manager.read_text(encoding="utf-8"))
            self.assertFalse(Path(description["files"]["private_receipt"]).exists())

    def test_producer_forged_post_pointer_ledger_line_cannot_mint_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"post-pointer forged acknowledgment\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            description = json.loads(run_report(case, describe=True).stdout)
            pending = run_report(case)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            state, key = acknowledgment_coordinates(description)
            state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            files = description["files"]
            assert isinstance(files, dict)
            pointer = f"(from agent cfg:7 {files['private_envelope']})"
            before = case.manager.read_bytes()
            after = before.replace(f"(pending)\n{pointer}\n".encode(), b"", 1)
            transition = "\t".join(
                (
                    f"{time.time():.6f}",
                    key,
                    "watcher-locked-pointer-transition-v1",
                    hashlib.sha256(str(case.manager.resolve()).encode()).hexdigest(),
                    hashlib.sha256(pointer.encode()).hexdigest(),
                    hashlib.sha256(before).hexdigest(),
                    str(len(before)),
                    hashlib.sha256(after).hexdigest(),
                    str(len(after)),
                )
            )
            state.write_text(f"{transition}\n", encoding="utf-8")
            state.chmod(0o600)

            forged = run_report(case)

            self.assertEqual(0, forged.returncode, forged.stderr)
            self.assertFalse(json.loads(forged.stdout)["accepted"])
            self.assertIn("(pending)", case.manager.read_text(encoding="utf-8"))
            self.assertFalse(Path(description["files"]["private_receipt"]).exists())

    def test_producer_forged_full_transition_and_post_state_lack_watcher_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"full forged watcher transition and post-state\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            description = json.loads(run_report(case, describe=True).stdout)
            pending = run_report(case)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            state, key = acknowledgment_coordinates(description)
            state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            files = description["files"]
            assert isinstance(files, dict)
            pointer = f"(from agent cfg:7 {files['private_envelope']})"
            before = case.manager.read_bytes()
            after = before.replace(f"(pending)\n{pointer}\n".encode(), b"", 1)
            authority = state.parent / "pending-watch-authority" / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
            authority.parent.mkdir(mode=0o700)
            authority.write_bytes(b"")
            authority.chmod(0o600)
            authority_info = authority.stat()
            process_stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
            start_ticks = int(process_stat[process_stat.rfind(") ") + 2 :].split()[19])
            source_path = OMO_DIR / "omo_task_lock.py"
            transition = "\t".join(
                (
                    f"{time.time():.6f}",
                    key,
                    "watcher-locked-pointer-transition-v1",
                    hashlib.sha256(str(case.manager.resolve()).encode()).hexdigest(),
                    hashlib.sha256(pointer.encode()).hexdigest(),
                    hashlib.sha256(before).hexdigest(),
                    str(len(before)),
                    hashlib.sha256(after).hexdigest(),
                    str(len(after)),
                    "watcher-consumption-authority-v1",
                    "bounded-watcher-lease",
                    str(os.getpid()),
                    str(start_ticks),
                    hashlib.sha256(str(authority).encode()).hexdigest(),
                    str(authority_info.st_dev),
                    str(authority_info.st_ino),
                    str(source_path),
                    hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    hashlib.sha256(("f" * 64).encode()).hexdigest(),
                )
            )
            state.write_text(f"{transition}\n", encoding="utf-8")
            state.chmod(0o600)
            forged_manager = case.manager.with_name(f".{case.manager.name}.producer-forged")
            forged_manager.write_bytes(after)
            forged_manager.chmod(case.manager.stat().st_mode & 0o7777)
            os.replace(forged_manager, case.manager)

            forged = run_report(case)

            self.assertEqual(2, forged.returncode)
            self.assertEqual("", forged.stdout)
            self.assertIn("manager bytes differ from the bound report transaction", forged.stderr)
            self.assertEqual(after, case.manager.read_bytes())
            self.assertFalse(Path(files["private_receipt"]).exists())

            case.manager.write_bytes(before)
            watcher = run_manager_watcher_once(case)
            self.assertEqual(0, watcher.returncode, watcher.stderr)
            accepted = run_report(case)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertTrue(json.loads(accepted.stdout)["accepted"])

    def test_stale_genuine_transition_with_active_pointer_remains_unaccepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"stale transition with active pointer\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            description = json.loads(run_report(case, describe=True).stdout)
            pending = run_report(case)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            watcher = run_manager_watcher_once(case)
            self.assertEqual(0, watcher.returncode, watcher.stderr)
            files = description["files"]
            assert isinstance(files, dict)
            pointer = f"(from agent cfg:7 {files['private_envelope']})"
            case.manager.write_text(case.manager.read_text(encoding="utf-8") + f"\n(pending)\n{pointer}\n", encoding="utf-8")

            replay = run_report(case)

            self.assertEqual(0, replay.returncode, replay.stderr)
            self.assertFalse(json.loads(replay.stdout)["accepted"])
            self.assertIn("(pending)", case.manager.read_text(encoding="utf-8"))
            self.assertFalse(Path(files["private_receipt"]).exists())

    def test_live_manager_hierarchy_routes_to_upper_manager_with_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"batch: B01\nattempt: attempt-0002\nprivate migration report\n", managerat="vlexp:5")
            producer = case.root / "vlexp_path_migration_exec.md"
            upper_manager = case.root / "vlexp_replenish.md"
            historical_manager = case.root / "vlexp_historical.md"
            (case.root / "worker.md").unlink()
            producer_text = frontmatter(runat="vlexp:13", managerat="vlexp:5", is_manager=True)
            producer.write_text(producer_text, encoding="utf-8")
            upper_manager_text = frontmatter(runat="vlexp:5", managerat="vlexp:1", is_manager=True)
            upper_manager.write_text(upper_manager_text, encoding="utf-8")
            historical_manager_text = frontmatter(runat="vlexp:5", managerat="vlexp:1", is_manager=True).replace(
                "status: running",
                "status: blocked\nblocked_on: historical manager task",
            )
            historical_manager.write_text(historical_manager_text, encoding="utf-8")
            noise_lines: list[str] = []
            for index in range(20, 40):
                name = f"unrelated_{index}.md"
                noise_lines.append(f"{name} other:{index}")
                (case.root / name).write_text(frontmatter(runat=f"other:{index}", managerat="main:0.0"), encoding="utf-8")
            (case.root / "TODO.md").write_text(
                "current:\nvlexp_path_migration_exec.md vlexp:13\nvlexp_replenish.md vlexp:5\nvlexp_historical.md vlexp:5\n"
                + "\n".join(noise_lines)
                + "\n",
                encoding="utf-8",
            )
            tmux = Path(case.env["PATH"].split(":", 1)[0]) / "tmux"
            tmux.write_text("#!/usr/bin/env bash\nprintf 'vlexp\\t13\\t0\\t%%1701\\tpath_migration_exec\\n'\n", encoding="utf-8")
            tmux.chmod(0o700)

            description = json.loads(run_report(case, describe=True).stdout)

            self.assertEqual(str(producer), description["routing"]["task"])
            self.assertEqual(str(upper_manager), description["routing"]["manager"])
            self.assertEqual("vlexp:5", description["routing"]["requested_manager_target"])
            self.assertEqual("vlexp:5", description["routing"]["resolved_manager_target"])
            self.assertEqual("active-manager-task", description["routing"]["route_kind"])
            self.assertNotIn("unrelated_20.md", json.dumps(description))
            self.assertLess(len(json.dumps(description).encode()), 4096)

            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report(case)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            manager_run = run_manager_watcher_once(case, upper_manager)
            self.assertEqual(0, manager_run.returncode, manager_run.stderr)
            self.assertEqual("vlexp:5", manager_run.stdout.strip())
            result = run_report(case)

            self.assertEqual(0, result.returncode, result.stderr)
            acceptance = json.loads(result.stdout)
            self.assertTrue(acceptance["accepted"])
            self.assertEqual("vlexp:5", acceptance["routing"]["resolved_manager_target"])
            self.assertEqual(producer_text, producer.read_text(encoding="utf-8"))
            self.assertEqual(upper_manager_text, upper_manager.read_text(encoding="utf-8"))
            self.assertEqual(historical_manager_text, historical_manager.read_text(encoding="utf-8"))
            self.assertNotIn("(pending)", producer.read_text(encoding="utf-8"))
            receipt = private_receipt(acceptance)
            self.addCleanup(cleanup_private_tmp, receipt)
            evidence_paths = {Path(str(item["path"])).name for item in receipt["routing"]["route_evidence"]}
            self.assertEqual({"TODO.md", "vlexp_historical.md", "vlexp_path_migration_exec.md", "vlexp_replenish.md"}, evidence_paths)

    def test_failure_and_missing_or_ambiguous_routes_emit_no_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            invalid = fixture(tmp_path, body=b"\xffprivate\n")
            failed = run_report(invalid)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual("", failed.stdout)
            self.assertFalse(invalid.manager.exists())
            self.assertFalse(Path(f"{invalid.manager}.omo_report.lock").exists())

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing = fixture(tmp_path)
            (missing.root / "TODO.md").write_text("current:\n", encoding="utf-8")
            failed = run_report(missing, describe=True)
            self.assertEqual(2, failed.returncode)
            self.assertEqual("", failed.stdout)
            self.assertFalse(missing.manager.exists())

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ambiguous = fixture(tmp_path, managerat="vl:2")
            (ambiguous.root / "TODO.md").write_text(
                "current:\nworker.md cfg:7\nmanager-a.md vl:2\nmanager-b.md vl:2\n",
                encoding="utf-8",
            )
            for name in ("manager-a.md", "manager-b.md"):
                (ambiguous.root / name).write_text(
                    frontmatter(runat="vl:2", managerat="main:0.0", is_manager=True),
                    encoding="utf-8",
                )
            failed = run_report(ambiguous, describe=True)
            self.assertEqual(2, failed.returncode)
            self.assertEqual("", failed.stdout)
            self.assertIn("multiple active manager task files", failed.stderr)
            self.assertFalse(ambiguous.manager.exists())

    def test_identical_concurrent_submissions_return_one_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"one concurrent report\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending_processes = [
                subprocess.Popen(
                    case.command(),
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            for process in pending_processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(0, process.returncode, stderr)
                self.assertFalse(json.loads(stdout)["accepted"])
            manager_run = run_manager_watcher_once(case)
            self.assertEqual(0, manager_run.returncode, manager_run.stderr)

            processes = [
                subprocess.Popen(
                    case.command(),
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            outputs: list[dict[str, object]] = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(0, process.returncode, stderr)
                self.assertEqual("", stderr)
                outputs.append(json.loads(stdout))
            self.addCleanup(cleanup_private_tmp, private_receipt(outputs[0]))
            self.assertEqual(1, len({str(output["receipt_id"]) for output in outputs}))
            self.assertNotIn("(pending)", case.manager.read_text(encoding="utf-8"))
            records = list((Path(tmp) / "state" / "omo-manager" / "report-receipts").glob("*.json"))
            self.assertEqual(2, len(records))

    def test_sequential_active_manager_retry_returns_original_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, managerat="vl:2")
            manager = case.root / "manager.md"
            (case.root / "TODO.md").write_text(
                "current:\nworker.md cfg:7\nmanager.md vl:2\n",
                encoding="utf-8",
            )
            manager.write_text(
                frontmatter(runat="vl:2", managerat="main:0.0", is_manager=True),
                encoding="utf-8",
            )

            _, first = run_accepted(case)
            second = run_report(case)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            receipt = private_receipt(json.loads(first.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            self.assertNotIn("(pending)", manager.read_text(encoding="utf-8"))
            records = list((tmp_path / "state" / "omo-manager" / "report-receipts").glob("*.json"))
            self.assertEqual(2, len(records))

    def test_same_input_from_a_new_private_draft_replays_original_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"stable replay input\n")
            _, first = run_accepted(case)
            self.assertEqual(0, first.returncode, first.stderr)

            replacement = case.message.with_name("replacement-message.md")
            replacement.write_bytes(case.message.read_bytes())
            replacement.chmod(0o600)
            command = case.command()
            command[command.index("--message-file") + 1] = str(replacement)
            second = subprocess.run(
                command,
                cwd=case.root.parent,
                env=case.env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(first.stdout, second.stdout)

    def test_post_receipt_publication_failure_recovers_on_identical_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, body=b"recover publication after durable receipt\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = "def commit_receipt_publication(plan: Plan, receipt_payload: bytes) -> bytes:\n    receipt = json.loads(receipt_payload)"
            injection = (
                "def commit_receipt_publication(plan: Plan, receipt_payload: bytes) -> bytes:\n"
                '    if os.environ.get("OMO_TEST_FAIL_PUBLICATION") == "1":\n'
                '        raise ReceiptError("injected publication failure")\n'
                "    receipt = json.loads(receipt_payload)"
            )
            self.assertIn(marker, source)
            receiver.write_text(source.replace(marker, injection, 1), encoding="utf-8")
            command = case.command()
            command[0] = str(report)
            describe_command = case.command(describe=True)
            describe_command[0] = str(report)
            description = json.loads(
                subprocess.run(
                    describe_command,
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=True,
                ).stdout
            )
            pending = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            watcher = run_manager_watcher_once(case, Path(str(description["files"]["manager"])))
            self.assertEqual(0, watcher.returncode, watcher.stderr)

            case.env["OMO_TEST_FAIL_PUBLICATION"] = "1"
            failed = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            case.env.pop("OMO_TEST_FAIL_PUBLICATION")
            self.assertEqual(2, failed.returncode)
            self.assertEqual("", failed.stdout)
            self.assertIn("injected publication failure", failed.stderr)
            receipt = Path(str(description["files"]["private_receipt"]))
            publication = Path(str(description["files"]["receipt_publication"]))
            self.assertTrue(receipt.is_file())
            self.assertFalse(publication.exists())

            recovered = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)

            self.assertEqual(0, recovered.returncode, recovered.stderr)
            acceptance = json.loads(recovered.stdout)
            self.assertTrue(acceptance["accepted"])
            self.assertTrue(publication.is_file())
            publication_payload = json.loads(publication.read_text(encoding="utf-8"))
            self.assertEqual(publication_payload["receipt_state"], acceptance["receipt_state"])
            self.assertEqual(recovered.stdout, subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=True).stdout)

    def test_post_publication_rename_failure_replay_fsyncs_existing_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path, body=b"recover renamed publication directory entry\n")
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            rename_marker = "        os.rename(plan.receipt_publication_temporary, plan.receipt_publication_final)\n"
            rename_injection = (
                rename_marker
                + '        if os.environ.get("OMO_TEST_FAIL_AFTER_PUBLICATION_RENAME") == "1":\n'
                + '            raise ReceiptError("injected post-rename publication failure")\n'
            )
            fsync_marker = "def fsync_directory(path: Path) -> None:\n    flags = "
            fsync_injection = (
                "def fsync_directory(path: Path) -> None:\n"
                '    audit = os.environ.get("OMO_TEST_PUBLICATION_FSYNC_AUDIT")\n'
                '    if audit and any(path.glob("*.publication.json")):\n'
                '        Path(audit).write_text("fsynced-existing-publication\\n", encoding="utf-8")\n'
                "    flags = "
            )
            self.assertIn(rename_marker, source)
            self.assertIn(fsync_marker, source)
            receiver.write_text(
                source.replace(rename_marker, rename_injection, 1).replace(fsync_marker, fsync_injection, 1),
                encoding="utf-8",
            )
            command = case.command()
            command[0] = str(report)
            describe_command = case.command(describe=True)
            describe_command[0] = str(report)
            description = json.loads(
                subprocess.run(
                    describe_command,
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=True,
                ).stdout
            )
            pending = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            watcher = run_manager_watcher_once(case, Path(str(description["files"]["manager"])))
            self.assertEqual(0, watcher.returncode, watcher.stderr)

            case.env["OMO_TEST_FAIL_AFTER_PUBLICATION_RENAME"] = "1"
            failed = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            case.env.pop("OMO_TEST_FAIL_AFTER_PUBLICATION_RENAME")
            self.assertEqual(2, failed.returncode)
            self.assertEqual("", failed.stdout)
            self.assertIn("injected post-rename publication failure", failed.stderr)
            receipt = Path(str(description["files"]["private_receipt"]))
            publication = Path(str(description["files"]["receipt_publication"]))
            self.assertTrue(receipt.is_file())
            self.assertTrue(publication.is_file())

            audit = tmp_path / "publication-fsync-audit"
            case.env["OMO_TEST_PUBLICATION_FSYNC_AUDIT"] = str(audit)
            recovered = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            case.env.pop("OMO_TEST_PUBLICATION_FSYNC_AUDIT")

            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertTrue(json.loads(recovered.stdout)["accepted"])
            self.assertEqual("fsynced-existing-publication\n", audit.read_text(encoding="utf-8"))

    def test_receipt_content_tampering_invalidates_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            _, first = run_accepted(case)
            self.assertEqual(0, first.returncode, first.stderr)
            acceptance = json.loads(first.stdout)
            receipt = private_receipt(acceptance)
            self.addCleanup(cleanup_private_tmp, receipt)
            record = Path(str(acceptance["receipt_path"]))
            tampered = json.loads(record.read_text(encoding="utf-8"))
            tampered["routing"]["route_evidence_sha256"] = "0" * 64
            record.write_text(
                json.dumps(tampered, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            record.chmod(0o600)

            retry = run_report(case)

            self.assertEqual(2, retry.returncode)
            self.assertEqual("", retry.stdout)
            self.assertIn("content binding is invalid", retry.stderr)
            self.assertNotIn("(pending)", case.manager.read_text(encoding="utf-8"))

    def test_legacy_verbose_marker_never_mints_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"legacy marker body\n")
            description = json.loads(run_report(case, describe=True).stdout)
            self.addCleanup(cleanup_private_tmp, description)
            digest = hashlib.sha256(case.message.read_bytes()).hexdigest()
            legacy = f"(pending)\n(from agent receipt-worker via omo_report.sh status=done)\n[message-sha256: {digest}]\n"
            case.manager.write_text(legacy, encoding="utf-8")

            result = run_report(case)

            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("legacy report marker", result.stderr)
            self.assertEqual(legacy, case.manager.read_text(encoding="utf-8"))
            self.assertFalse(Path(description["files"]["private_receipt"]).exists())
            self.assertFalse(Path(description["files"]["private_envelope"]).exists())

    def test_route_change_between_resolution_and_receiver_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path)
            description = json.loads(run_report(case, describe=True).stdout)
            self.addCleanup(cleanup_private_tmp, description)
            replacement = tmp_path / "replacement.md"
            replacement.write_text(frontmatter(runat="cfg:7", managerat="other:9"), encoding="utf-8")
            python_wrapper = tmp_path / "bin" / "python3"
            python_wrapper.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        'if [ "${OMO_REPORT_RECEIVER_BOOTSTRAP:-}" = "1" ]; then',
                        '  cp -- "$ROUTE_MUTATION_SOURCE" "$ROUTE_MUTATION_DEST"',
                        "fi",
                        f'exec {shlex.quote(sys.executable)} "$@"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            python_wrapper.chmod(0o700)
            case.env["ROUTE_MUTATION_SOURCE"] = str(replacement)
            case.env["ROUTE_MUTATION_DEST"] = str(case.root / "worker.md")

            result = run_report(case)

            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("route evidence changed", result.stderr)
            self.assertFalse(case.manager.exists())
            self.assertFalse(Path(description["files"]["private_receipt"]).exists())
            self.assertFalse(Path(description["files"]["private_envelope"]).exists())

    def test_dependency_identity_changes_receipt_replay_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path)
            report = copy_report_helper(tmp_path)
            command = case.command()
            command[0] = str(report)

            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, pending.returncode, pending.stderr)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            manager_run = run_manager_watcher_once(case)
            self.assertEqual(0, manager_run.returncode, manager_run.stderr)
            first = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, first.returncode, first.stderr)
            first_acceptance = json.loads(first.stdout)
            first_receipt = private_receipt(first_acceptance)
            dependency = report.parent / "omo_pending_digest.py"
            dependency.write_text(dependency.read_text(encoding="utf-8") + "\n# identity change\n", encoding="utf-8")
            second = subprocess.run(command, cwd=case.root.parent, env=case.env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, second.returncode, second.stderr)
            second_acceptance = json.loads(second.stdout)
            second_receipt = private_receipt(second_acceptance)
            self.addCleanup(cleanup_private_tmp, second_receipt)

            self.assertNotEqual(first_acceptance["receipt_id"], second_acceptance["receipt_id"])
            self.assertNotEqual(
                first_receipt["helper"]["dependencies"]["omo_pending_digest"]["sha256"],
                second_receipt["helper"]["dependencies"]["omo_pending_digest"]["sha256"],
            )
            self.assertNotIn("(pending)", case.manager.read_text(encoding="utf-8"))
            records = list((tmp_path / "state" / "omo-manager" / "report-receipts").glob("*.json"))
            self.assertEqual(4, len(records))

    def test_dependency_edit_after_loading_never_claims_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path)
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = "from .omo_task_lock import watcher_report_authority_is_live\n"
            self.assertIn(marker, source)
            receiver.write_text(
                source.replace(
                    marker,
                    marker + '\nPath(__file__).with_name("omo_pending_digest.py").write_text(' + '"# changed after loading\\n", encoding="utf-8")\n',
                    1,
                ),
                encoding="utf-8",
            )
            command = case.command()
            command[0] = str(report)

            result = subprocess.run(
                command,
                cwd=case.root.parent,
                env=case.env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("executed helper dependency", result.stderr)
            self.assertFalse(case.manager.exists())
            self.assertFalse((tmp_path / "state").exists())

    def test_task_lock_dependency_edit_after_loading_never_claims_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            case = fixture(tmp_path)
            report = copy_report_helper(tmp_path)
            receiver = report.parent / "omo_report_receipt.py"
            source = receiver.read_text(encoding="utf-8")
            marker = "from .omo_task_lock import watcher_report_authority_is_live\n"
            self.assertIn(marker, source)
            receiver.write_text(
                source.replace(
                    marker,
                    marker + '\nPath(__file__).with_name("omo_task_lock.py").write_text(' + '"# changed after loading\\n", encoding="utf-8")\n',
                    1,
                ),
                encoding="utf-8",
            )
            command = case.command()
            command[0] = str(report)

            result = subprocess.run(
                command,
                cwd=case.root.parent,
                env=case.env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("executed helper dependency omo_task_lock", result.stderr)
            self.assertFalse(case.manager.exists())
            self.assertFalse((tmp_path / "state").exists())

    def test_duplicate_batch_or_attempt_context_is_rejected(self) -> None:
        for body in (
            b"batch: B01\nbatch: B01\n",
            b"attempt: attempt-0002\nattempt: attempt-0002\n",
            b"batch: B01\nbatch: B02\n",
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                case = fixture(Path(tmp), body=body)

                result = run_report(case)

                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn("duplicate", result.stderr)
                self.assertFalse(case.manager.exists())

    def test_adjacent_lock_directory_before_state_precedes_lock_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
            pending = run_report(case)
            self.assertFalse(json.loads(pending.stdout)["accepted"])
            watcher = run_manager_watcher_once(case)
            self.assertEqual(0, watcher.returncode, watcher.stderr)
            before_digest = entry_name_sha256(case.manager.parent)

            result = run_report(case)

            self.assertEqual(0, result.returncode, result.stderr)
            receipt = private_receipt(json.loads(result.stdout))
            self.addCleanup(cleanup_private_tmp, receipt)
            adjacent = receipt["side_effects"]["locks"]["adjacent_report"]
            self.assertEqual(before_digest, adjacent["directory_before"]["entry_name_sha256"])
            self.assertTrue(adjacent["before"]["exists"])
            self.assertTrue(adjacent["after"]["exists"])

    def test_incomplete_helper_side_effect_never_claims_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), body=b"residue must fail closed\n")
            description = json.loads(run_report(case, describe=True).stdout)
            self.addCleanup(cleanup_private_tmp, description)
            envelope_directory = Path(description["files"]["private_envelope"]).parent
            envelope_directory.mkdir(mode=0o700, exist_ok=True)
            envelope_directory.chmod(0o700)
            residue = next(Path(path) for path in description["temporary_files"] if "omo-agent-messages" in path)
            residue.write_bytes(b"interrupted")
            residue.chmod(0o600)
            self.addCleanup(residue.unlink, missing_ok=True)

            result = run_report(case)

            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("incomplete transaction residue", result.stderr)
            self.assertFalse(case.manager.exists())
            self.assertFalse(Path(description["files"]["private_receipt"]).exists())

    def test_post_ack_declared_producer_temporary_residue_blocks_receipt(self) -> None:
        for residue_kind in ("manager", "envelope", "receipt", "publication"):
            with self.subTest(residue_kind=residue_kind), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                case = fixture(tmp_path, body=f"post-ack {residue_kind} residue\n".encode())
                original = f"owner bytes for {residue_kind}".encode()
                case.manager.write_bytes(original)
                report = copy_report_helper(tmp_path)
                receiver = report.parent / "omo_report_receipt.py"
                source = receiver.read_text(encoding="utf-8")
                marker = (
                    "            acknowledgment_state_after = path_state(plan.acknowledgment_state)\n"
                    "            validate_private_layout(plan)\n"
                )
                injection = (
                    "            acknowledgment_state_after = path_state(plan.acknowledgment_state)\n"
                    '            injected_residue = os.environ.get("OMO_TEST_POST_ACK_RESIDUE")\n'
                    "            if injected_residue:\n"
                    '                Path(injected_residue).write_bytes(b"injected-residue")\n'
                    "                Path(injected_residue).chmod(0o600)\n"
                    "            validate_private_layout(plan)\n"
                )
                self.assertIn(marker, source)
                receiver.write_text(source.replace(marker, injection, 1), encoding="utf-8")
                command = case.command()
                command[0] = str(report)
                describe_command = case.command(describe=True)
                describe_command[0] = str(report)
                description = json.loads(
                    subprocess.run(
                        describe_command,
                        cwd=case.root.parent,
                        env=case.env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=True,
                    ).stdout
                )
                case.env["OMO_REPORT_ACK_TIMEOUT_S"] = "0"
                pending = subprocess.run(
                    command,
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(0, pending.returncode, pending.stderr)
                pending_result = json.loads(pending.stdout)
                self.assertFalse(pending_result["accepted"])
                transfer = pending_result["transfer_receipt"]
                self.assertEqual("omo-report-transfer-receipt/v1", transfer["schema"])
                self.assertEqual("agent-originated", transfer["authority"]["kind"])
                watcher = run_manager_watcher_once(case, Path(str(description["files"]["manager"])))
                self.assertEqual(0, watcher.returncode, watcher.stderr)

                files = description["files"]
                self.assertIsInstance(files, dict)
                temporary_files = [Path(str(path)) for path in description["temporary_files"]]
                manager = Path(str(files["manager"]))
                envelope = Path(str(files["private_envelope"]))
                receipt = Path(str(files["private_receipt"]))
                publication = Path(str(files["receipt_publication"]))
                residues = {
                    "manager": next(path for path in temporary_files if path.parent == manager.parent and ".omo-report-" in path.name),
                    "envelope": next(path for path in temporary_files if path.parent == envelope.parent),
                    "receipt": next(path for path in temporary_files if path.parent == receipt.parent and path.name == f".{receipt.stem}.tmp"),
                    "publication": next(path for path in temporary_files if path.parent == publication.parent and path.name.endswith(".publication.tmp")),
                }
                residue = residues[residue_kind]
                residue.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                case.env["OMO_TEST_POST_ACK_RESIDUE"] = str(residue)
                failed = subprocess.run(
                    command,
                    cwd=case.root.parent,
                    env=case.env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                case.env.pop("OMO_TEST_POST_ACK_RESIDUE")

                self.assertEqual(2, failed.returncode)
                self.assertEqual("", failed.stdout)
                self.assertIn("incomplete transaction residue", failed.stderr)
                self.assertEqual(original, manager.read_bytes())
                self.assertEqual(b"injected-residue", residue.read_bytes())
                self.assertFalse(receipt.exists())
                self.assertFalse(publication.exists())
                self.assertEqual(str(transaction_commitment_path(description)), transfer["commitment_path"])
                self.assertTrue(transaction_commitment_path(description).is_file())
                if residue_kind in {"receipt", "publication"}:
                    verified = subprocess.run(
                        [*command[:1], "--verify-consumed", *command[1:]],
                        cwd=case.root.parent,
                        env=case.env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(0, verified.returncode, verified.stderr)
                    closure = json.loads(verified.stdout)
                    self.assertEqual("omo-report-consumed-closure/v1", closure["schema"])
                    self.assertEqual(transfer, closure["transfer_receipt"])
                    self.assertFalse(closure["accepted"])
                    self.assertEqual(str(residue), closure["recovery_residue"][0]["path"])
                    self.assertEqual(hashlib.sha256(b"injected-residue").hexdigest(), closure["recovery_residue"][0]["state"]["sha256"])
                    replayed = subprocess.run(
                        [*command[:1], "--verify-consumed", *command[1:]],
                        cwd=case.root.parent,
                        env=case.env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(verified.stdout, replayed.stdout)
                residue.unlink()


if __name__ == "__main__":
    unittest.main()
