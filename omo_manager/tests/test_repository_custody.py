from __future__ import annotations

# pyright: reportUninitializedInstanceVariable=false

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import final, override
from unittest.mock import patch

from omo_manager.omo_repository_custody import ACCEPTANCE_SCHEMA
from omo_manager.omo_repository_custody import REVIEW_SCHEMA
from omo_manager.omo_repository_custody import CustodyError
from omo_manager.omo_repository_custody import PrepareArgs
from omo_manager.omo_repository_custody import TargetIdentity
from omo_manager.omo_repository_custody import canonical_json
from omo_manager.omo_repository_custody import digest
from omo_manager.omo_repository_custody import execute
from omo_manager.omo_repository_custody import link_descriptor_noreplace
from omo_manager.omo_repository_custody import prepare
from omo_manager.omo_repository_custody import target_identity
from omo_manager.omo_repository_custody import transaction_key
from omo_manager.omo_repository_custody import validate_review
from omo_manager.omo_repository_custody import write_new_private
from omo_manager.omo_report_receipt import TRANSACTION_COMMITMENT_SCHEMA, bound_receipt_id


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@final
class RepositoryCustodyTests(unittest.TestCase):
    temp: tempfile.TemporaryDirectory[str]
    base: Path
    repo: Path
    private: Path
    tasks: Path
    sources: tuple[str, ...]
    source_task: Path
    destination_task: Path
    authority: Path
    receipt: Path
    acceptance: Path
    state: Path

    @override
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        executor_patch = patch("omo_manager.omo_repository_custody.executor_belongs_to_target", return_value=True)
        _ = executor_patch.start()
        self.addCleanup(executor_patch.stop)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.private = self.base / "private"
        self.tasks = self.base / "tasks"
        self.repo.mkdir()
        self.private.mkdir(mode=0o700)
        self.tasks.mkdir()
        self.state = self.private / "state"
        self.state.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-b", "main", self.repo], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "Test"], check=True)
        (self.repo / "tracked").write_text("base\n")
        subprocess.run(["git", "-C", self.repo, "add", "tracked"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-m", "base"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "remote", "add", "origin", str(self.repo)], check=True)
        subprocess.run(["git", "-C", self.repo, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        subprocess.run(
            ["git", "-C", self.repo, "branch", "--set-upstream-to", "origin/main"],
            check=True,
            capture_output=True,
        )
        self.sources = ("one.py", "sub/two.md")
        (self.repo / "sub").mkdir()
        (self.repo / "one.py").write_bytes(b"one\n")
        (self.repo / "sub/two.md").write_bytes(b"two\n")
        self.source_task = self.tasks / "source.md"
        self.destination_task = self.tasks / "destination.md"
        self.write_task(self.source_task, "old:1", "blocked")
        self.write_task(self.destination_task, "new:1", "long_running", session_id="01234567-89ab-cdef-0123-456789abcdef")
        self.todo = self.tasks / "TODO.md"
        self.todo.write_text("source.md old:1\ndestination.md new:1\n")
        self.authority = self.private / "authority.txt"
        self.receipt = self.private / "receipt.txt"
        self.acceptance = self.private / "acceptance.json"
        self.authority.write_text("Human authority\n")
        self.write_report(
            self.receipt,
            "old:1",
            self.source_task,
            "\n".join(
                f"{self.repo / path}: SHA-256 {sha((self.repo / path).read_bytes())}" for path in self.sources
            )
            + "\n",
        )
        for path in (self.authority, self.receipt):
            path.chmod(0o600)
        self.write_acceptance()

    def write_task(self, path: Path, target: str, status: str, *, session_id: str = "") -> None:
        session = f"session_id: {session_id}\n" if session_id else ""
        blocker = "blocked_on: frozen source owner\n" if status == "blocked" else ""
        path.write_text(
            f"""---
version: v1.0.0
status: {status}
{blocker}runat: {target}
tool: codex
managerat: mgr:1
is_manager: false
pending_task_items: []
{session}---
task
"""
        )

    def write_acceptance(self) -> None:
        self.acceptance.write_bytes(
            canonical_json(
                {
                    "schema": ACCEPTANCE_SCHEMA,
                    "accepted": True,
                    "source_owner": "source-owner",
                    "destination_owner": "destination-owner",
                    "source_task": str(self.source_task.resolve()),
                    "destination_task": str(self.destination_task.resolve()),
                    "source_target": "old:1",
                    "destination_target": "new:1",
                    "repository": str(self.repo.resolve()),
                    "todo": {
                        "path": str(self.todo.resolve()),
                        "sha256": sha(self.todo.read_bytes()),
                        "source_row": "source.md old:1",
                        "destination_row": "destination.md new:1",
                    },
                    "files": [
                        {"path": path, "sha256": sha((self.repo / path).read_bytes())}
                        for path in self.sources
                    ],
                    "source_receipts": [
                        {
                            "path": str(self.receipt.resolve()),
                            "sha256": sha(self.receipt.read_bytes()),
                            "producer_target": "old:1",
                            "source_task": str(self.source_task),
                        }
                    ],
                }
            )
        )
        self.acceptance.chmod(0o600)

    def write_report(self, path: Path, producer: str, task: Path, body: str) -> None:
        replay = sha(body.encode())
        commitment_path = self.private / f"{path.stem}.commitment"
        contract: dict[str, object] = {
            "schema": "omo-report-transfer-receipt/v1",
            "authority": {"kind": "agent-originated", "producer_target": producer, "source_task": str(task)},
            "commitment_path": str(commitment_path),
            "queue_item": {"input_sha256": sha(body.encode()), "replay_id": replay},
            "receiver": str(self.destination_task),
            "routing": {"producer_target": producer, "task": str(task)},
        }
        commitment: dict[str, object] = {
            "schema": TRANSACTION_COMMITMENT_SCHEMA,
            "replay_id": replay,
            "allocation": {},
            "commitment": {},
            "preflight": {},
            "transfer": contract,
        }
        commitment["commitment_id"] = bound_receipt_id(commitment)
        commitment_path.write_bytes(canonical_json(commitment))
        commitment_path.chmod(0o600)
        transfer = {**contract, "commitment_id": commitment["commitment_id"]}
        transfer["transfer_id"] = bound_receipt_id(transfer)
        path.write_text(
            f"""(sent from agent via omo_report.sh tmux={producer} time=now task-file={task.name})
[message-sha256: {sha(body.encode())}]
[omo-transfer: {json.dumps(transfer, sort_keys=True, separators=(',', ':'))}]
message:
{body}"""
        )
        path.chmod(0o600)

    def args(self, output: Path | None = None) -> PrepareArgs:
        return PrepareArgs(
            self.repo,
            self.sources,
            tuple(sha((self.repo / path).read_bytes()) for path in self.sources),
            "source-owner",
            "destination-owner",
            self.source_task,
            sha(self.source_task.read_bytes()),
            "old:1",
            self.destination_task,
            sha(self.destination_task.read_bytes()),
            "new:1",
            self.todo,
            sha(self.todo.read_bytes()),
            self.authority,
            sha(self.authority.read_bytes()),
            (self.receipt,),
            (sha(self.receipt.read_bytes()),),
            self.acceptance,
            sha(self.acceptance.read_bytes()),
            output or self.private / "binding.json",
        )

    def target(self, target: str) -> TargetIdentity:
        pane = 1 if target == "old:1" else 2
        canonical = target if "." in target.partition(":")[2] else f"{target}.0"
        return TargetIdentity(canonical, f"%{pane}", 1000 + pane, pane, "bunx")

    def prepare_binding(self) -> Path:
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            prepare(self.args())
        return self.private / "binding.json"

    def review(self, binding: Path) -> Path:
        review = self.private / "review.json"
        self.write_report(
            review,
            "review:1",
            self.tasks / "review.md",
            canonical_json(
                {
                    "schema": REVIEW_SCHEMA,
                    "verdict": "PASS",
                    "binding_sha256": sha(binding.read_bytes()),
                    "notes": "complete diff reviewed",
                }
            ).decode(),
        )
        return review

    def execute_binding(
        self,
        binding: Path,
        *,
        before_publish: Callable[[], None] | None = None,
        after_publish: Callable[[], None] | None = None,
        crash_after: str = "",
    ) -> str:
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            return execute(
                binding,
                self.review(binding),
                state_root=self.state,
                before_publish=before_publish,
                after_publish=after_publish,
                crash_after=crash_after,
            )

    def test_commit_and_replay_change_only_ledger_outputs(self) -> None:
        before_files = tuple((self.repo / path).read_bytes() for path in self.sources)
        before_git = subprocess.run(
            ["git", "-C", self.repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=True,
            capture_output=True,
        ).stdout
        binding = self.prepare_binding()
        first = self.execute_binding(binding)
        self.assertEqual(first, self.execute_binding(binding))
        self.assertEqual(before_files, tuple((self.repo / path).read_bytes() for path in self.sources))
        after_git = subprocess.run(
            ["git", "-C", self.repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(before_git, after_git)
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        self.assertTrue((self.state / f"{transaction_key(parsed)}.committed.json").exists())

    def test_private_publication_has_no_replaceable_temporary_path(self) -> None:
        output = self.private / "anonymous-publication.json"
        observed_entries: list[str] = []

        def inspect_and_link(descriptor: int, parent_descriptor: int, name: str) -> None:
            self.assertEqual(os.fstat(descriptor).st_nlink, 0)
            observed_entries.extend(os.listdir(parent_descriptor))
            link_descriptor_noreplace(descriptor, parent_descriptor, name)

        with patch(
            "omo_manager.omo_repository_custody.link_descriptor_noreplace",
            side_effect=inspect_and_link,
        ):
            write_new_private(output, b"owned\n")
        self.assertEqual(output.read_bytes(), b"owned\n")
        self.assertFalse(any(name.startswith(f".{output.name}.") for name in observed_entries))

    def test_repository_relative_source_attestation_is_accepted(self) -> None:
        self.write_report(
            self.receipt,
            "old:1",
            self.source_task,
            "\n".join(
                f"  - {path}: SHA-256 {sha((self.repo / path).read_bytes())}" for path in self.sources
            )
            + "\n",
        )
        self.write_acceptance()
        binding = self.prepare_binding()
        self.assertTrue(binding.is_file())

    def test_same_uid_final_window_substitution_fails(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)

        def replace_path() -> None:
            replacement = self.repo / "replacement"
            replacement.write_bytes(b"one\n")
            os.replace(replacement, self.repo / "one.py")

        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "identity drifted"):
                execute(
                    binding,
                    review,
                    state_root=self.state,
                    before_publish=replace_path,
                )
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        self.assertFalse((self.state / f"{transaction_key(parsed)}.ledger.json").exists())

    def swap_source_ancestor(self) -> None:
        old_parent = self.repo / "old-sub"
        os.replace(self.repo / "sub", old_parent)
        (self.repo / "sub").mkdir()
        os.replace(old_parent / "two.md", self.repo / "sub/two.md")

    def test_source_ancestor_swap_fails_before_ledger(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "source ancestor identity drifted"):
                execute(binding, review, state_root=self.state, before_publish=self.swap_source_ancestor)
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        self.assertFalse((self.state / f"{transaction_key(parsed)}.ledger.json").exists())

    def test_source_ancestor_swap_after_ledger_is_indeterminate(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "indeterminate custody transaction"):
                execute(binding, review, state_root=self.state, after_publish=self.swap_source_ancestor)
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        key = transaction_key(parsed)
        self.assertTrue((self.state / f"{key}.ledger.json").exists())
        self.assertFalse((self.state / f"{key}.committed.json").exists())

    def swap_task_ancestor(self) -> None:
        old_tasks = self.base / "old-tasks"
        os.replace(self.tasks, old_tasks)
        self.tasks.mkdir()
        for child in tuple(old_tasks.iterdir()):
            os.replace(child, self.tasks / child.name)

    def test_task_todo_ancestor_swap_fails_before_ledger(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "input ancestor identity drifted"):
                execute(binding, review, state_root=self.state, before_publish=self.swap_task_ancestor)
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        self.assertFalse((self.state / f"{transaction_key(parsed)}.ledger.json").exists())

    def test_task_todo_ancestor_swap_after_ledger_is_indeterminate(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "indeterminate custody transaction"):
                execute(binding, review, state_root=self.state, after_publish=self.swap_task_ancestor)
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        key = transaction_key(parsed)
        self.assertTrue((self.state / f"{key}.ledger.json").exists())
        self.assertFalse((self.state / f"{key}.committed.json").exists())

    def test_authority_ancestor_swap_before_hold_is_rejected(self) -> None:
        evidence = self.base / "evidence"
        evidence.mkdir(mode=0o700)
        moved_authority = evidence / self.authority.name
        os.replace(self.authority, moved_authority)
        self.authority = moved_authority
        binding = self.prepare_binding()
        review = self.review(binding)
        old_evidence = self.base / "old-evidence"
        os.replace(evidence, old_evidence)
        evidence.mkdir(mode=0o700)
        os.replace(old_evidence / moved_authority.name, moved_authority)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "input ancestor identity drifted"):
                execute(binding, review, state_root=self.state)

    def test_review_ancestor_swap_between_authentication_and_hold_is_rejected(self) -> None:
        binding = self.prepare_binding()
        review_directory = self.private / "review-directory"
        review_directory.mkdir(mode=0o700)
        review = self.review_at(binding, review_directory)

        def validate_then_swap(
            report_data: bytes,
            parsed_binding: dict[str, object],
            binding_sha256: str,
        ) -> dict[str, object]:
            result = validate_review(report_data, parsed_binding, binding_sha256)
            old_directory = self.private / "old-review-directory"
            os.replace(review_directory, old_directory)
            review_directory.mkdir(mode=0o700)
            os.replace(old_directory / review.name, review)
            return result

        with (
            patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target),
            patch("omo_manager.omo_repository_custody.validate_review", side_effect=validate_then_swap),
        ):
            with self.assertRaisesRegex(CustodyError, "input ancestor identity drifted"):
                execute(binding, review, state_root=self.state)

    def test_todo_membership_drift_fails_before_execution(self) -> None:
        binding = self.prepare_binding()
        self.todo.write_text("source.md other:1\ndestination.md new:1\n")
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "TODO index digest changed|held input"):
                execute(binding, self.review(binding), state_root=self.state)

    def test_missing_exact_todo_row_fails_prepare(self) -> None:
        self.todo.write_text("source.md other:1\ndestination.md new:1\n")
        self.write_acceptance()
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "each exact custody row once"):
                prepare(self.args())

    def test_post_publication_substitution_is_indeterminate(self) -> None:
        binding = self.prepare_binding()

        def replace_path() -> None:
            replacement = self.repo / "replacement"
            replacement.write_bytes(b"one\n")
            os.replace(replacement, self.repo / "one.py")

        with self.assertRaisesRegex(CustodyError, "indeterminate"):
            self.execute_binding(binding, after_publish=replace_path)
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        key = transaction_key(parsed)
        self.assertTrue((self.state / f"{key}.ledger.json").exists())
        self.assertFalse((self.state / f"{key}.committed.json").exists())

    def test_report_commitment_replacement_is_descriptor_guarded(self) -> None:
        binding = self.prepare_binding()
        commitment = self.private / "receipt.commitment"

        def replace_commitment() -> None:
            replacement = self.private / "replacement-commitment"
            replacement.write_bytes(commitment.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, commitment)

        with self.assertRaisesRegex(CustodyError, "identity drifted"):
            self.execute_binding(binding, before_publish=replace_commitment)

    def test_review_envelope_post_publication_replacement_is_indeterminate(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)

        def replace_review() -> None:
            replacement = self.private / "replacement-review"
            replacement.write_bytes(review.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, review)

        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "indeterminate"):
                execute(binding, review, state_root=self.state, after_publish=replace_review)

    def test_path_symlink_and_hardlink_are_rejected(self) -> None:
        original = self.repo / "one.py"
        link = self.repo / "hard"
        os.link(original, link)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "unambiguous regular"):
                prepare(self.args())
        link.unlink()
        original.unlink()
        original.symlink_to(self.repo / "sub/two.md")
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaises(CustodyError):
                prepare(self.args())

    def test_tracked_or_staged_source_is_rejected(self) -> None:
        subprocess.run(["git", "-C", self.repo, "add", "one.py"], check=True)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "not exactly untracked"):
                prepare(self.args())

    def test_same_live_target_is_rejected(self) -> None:
        args = replace(self.args(), destination_target="old:1")
        value: dict[str, object] = json.loads(self.acceptance.read_bytes())
        value["destination_target"] = "old:1"
        self.acceptance.write_bytes(canonical_json(value))
        args = replace(args, acceptance_sha256=sha(self.acceptance.read_bytes()))
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "must differ"):
                prepare(args)

    def test_repository_status_and_index_drift_fail(self) -> None:
        binding = self.prepare_binding()
        (self.repo / "extra").write_text("extra")
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "drifted"):
                execute(binding, self.review(binding), state_root=self.state)
        (self.repo / "extra").unlink()
        (self.repo / "tracked").write_text("changed\n")
        subprocess.run(["git", "-C", self.repo, "add", "tracked"], check=True)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "drifted"):
                execute(binding, self.review(binding), state_root=self.state)

    def test_repository_remote_and_tracking_relationship_drift_fail(self) -> None:
        binding = self.prepare_binding()
        subprocess.run(["git", "-C", self.repo, "remote", "set-url", "origin", str(self.base / "other")], check=True)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "drifted"):
                execute(binding, self.review(binding), state_root=self.state)

        subprocess.run(["git", "-C", self.repo, "remote", "set-url", "origin", str(self.repo)], check=True)
        tracking = self.private / "tracking"
        tracking.mkdir(mode=0o700)
        binding = self.prepare_binding_for(tracking)
        subprocess.run(["git", "-C", self.repo, "update-ref", "refs/remotes/origin/other", "HEAD"], check=True)
        subprocess.run(
            ["git", "-C", self.repo, "branch", "--set-upstream-to", "origin/other"],
            check=True,
            capture_output=True,
        )
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "drifted"):
                execute(binding, self.review_at(binding, tracking), state_root=self.state)

    def test_binding_records_remote_tracking_and_divergence(self) -> None:
        binding = self.prepare_binding()
        repository = json.loads(binding.read_bytes())["repository"]
        self.assertEqual("refs/remotes/origin/main", repository["upstream_ref"])
        self.assertEqual("origin", repository["upstream_remote"])
        self.assertEqual(str(self.repo), repository["remote_url"])
        self.assertEqual(0, repository["ahead"])
        self.assertEqual(0, repository["behind"])

    def test_prepared_and_ledger_crashes_recover_idempotently(self) -> None:
        for phase in ("prepared", "ledger"):
            with self.subTest(phase=phase):
                directory = self.private / phase
                directory.mkdir(mode=0o700)
                binding = self.prepare_binding_for(directory)
                review = self.review_at(binding, directory)
                state = directory / "state"
                state.mkdir(mode=0o700)
                with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
                    with self.assertRaisesRegex(CustodyError, "injected crash"):
                        execute(binding, review, state_root=state, crash_after=phase)
                    result = execute(binding, review, state_root=state)
                    self.assertEqual(result, execute(binding, review, state_root=state))

    def prepare_binding_for(self, directory: Path) -> Path:
        binding = directory / "binding.json"
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            prepare(self.args(binding))
        return binding

    def review_at(self, binding: Path, directory: Path) -> Path:
        review = directory / "review.json"
        self.write_report(
            review,
            "review:1",
            self.tasks / "review.md",
            canonical_json(
                {
                    "schema": REVIEW_SCHEMA,
                    "verdict": "PASS",
                    "binding_sha256": digest(binding.read_bytes()),
                    "notes": "complete diff reviewed",
                }
            ).decode(),
        )
        return review

    def test_same_owner_and_wrong_expected_receipt_digest_fail(self) -> None:
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "must differ"):
                prepare(replace(self.args(), destination_owner="source-owner"))
            with self.assertRaisesRegex(CustodyError, "digest changed"):
                prepare(replace(self.args(), source_receipt_sha256s=("0" * 64,)))

    def test_wrong_acceptance_and_receipt_fail_without_output(self) -> None:
        self.write_acceptance()
        value: dict[str, object] = json.loads(self.acceptance.read_bytes())
        value["destination_owner"] = "wrong"
        self.acceptance.write_bytes(canonical_json(value))
        args = self.args()
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "does not exactly accept"):
                prepare(args)
        self.assertFalse(args.output.exists())
        self.write_acceptance()
        self.write_report(self.receipt, "old:1", self.source_task, "does not bind files\n")
        self.write_acceptance()
        args = self.args()
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "does not bind path"):
                prepare(args)

    def test_source_receipt_wrong_target_or_task_fails(self) -> None:
        body = "\n".join(
            f"{self.repo / path}: SHA-256 {sha((self.repo / path).read_bytes())}" for path in self.sources
        ) + "\n"
        self.write_report(self.receipt, "other:1", self.source_task, body)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "bound source owner"):
                prepare(self.args())
        other_task = self.tasks / "other-source.md"
        self.write_task(other_task, "old:1", "blocked")
        self.write_report(self.receipt, "old:1", other_task, body)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "bound source owner"):
                prepare(self.args())

    def test_prepare_ignores_path_git_substitution(self) -> None:
        attacker = self.base / "attacker-bin"
        attacker.mkdir()
        marker = self.base / "path-git-ran"
        fake_git = attacker / "git"
        fake_git.write_text(f"#!/bin/sh\ntouch {marker}\nexit 91\n")
        fake_git.chmod(0o755)
        with (
            patch.dict(os.environ, {"PATH": str(attacker)}),
            patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target),
        ):
            _ = prepare(self.args())
        self.assertFalse(marker.exists())

    def test_target_identity_ignores_path_tmux_substitution(self) -> None:
        attacker = self.base / "attacker-tmux-bin"
        attacker.mkdir()
        marker = self.base / "path-tmux-ran"
        fake_tmux = attacker / "tmux"
        fake_tmux.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        fake_tmux.chmod(0o755)
        with (
            patch.dict(os.environ, {"PATH": str(attacker)}),
            self.assertRaises(CustodyError),
        ):
            _ = target_identity("definitely-absent:999")
        self.assertFalse(marker.exists())

    def test_target_identity_uses_pinned_no_start_tmux_and_parses_success(self) -> None:
        completed = subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=r"unit:1.0\t%99\t4242\tbunx" + "\n",
            stderr="",
        )
        with (
            patch("omo_manager.omo_repository_custody.subprocess.run", return_value=completed) as run,
            patch("omo_manager.omo_repository_custody.process_start_ticks", return_value=12345),
        ):
            identity = target_identity("unit:1")
        self.assertEqual(identity, TargetIdentity("unit:1.0", "%99", 4242, 12345, "bunx"))
        invocation = run.call_args
        arguments = invocation.args[0]
        self.assertTrue(arguments[0].startswith("/proc/self/fd/"))
        self.assertEqual(arguments[1:5], ["-N", "-S", "/tmp/tmux-30033/default", "list-panes"])
        self.assertEqual(invocation.kwargs["executable"], arguments[0])
        self.assertNotIn("PATH", invocation.kwargs["env"])
        self.assertEqual(len(invocation.kwargs["pass_fds"]), 1)

    def test_target_identity_absent_server_fails_without_start_retry(self) -> None:
        completed = subprocess.CompletedProcess(args=(), returncode=1, stdout="", stderr="no server")
        with patch("omo_manager.omo_repository_custody.subprocess.run", return_value=completed) as run:
            with self.assertRaisesRegex(CustodyError, "cannot inspect tmux"):
                _ = target_identity("absent:1")
        self.assertEqual(run.call_count, 1)
        self.assertIn("-N", run.call_args.args[0])

    def test_target_identity_missing_socket_fails_before_tmux(self) -> None:
        with (
            patch("omo_manager.omo_repository_custody.os.stat", side_effect=FileNotFoundError),
            patch("omo_manager.omo_repository_custody.subprocess.run") as run,
        ):
            with self.assertRaisesRegex(CustodyError, "cannot inspect the trusted tmux socket"):
                _ = target_identity("absent:1")
        run.assert_not_called()

    def test_target_identity_socket_disappearance_is_controlled(self) -> None:
        socket_identity = os.stat("/tmp/tmux-30033/default", follow_symlinks=False)
        completed = subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=r"unit:1.0\t%99\t4242\tbunx" + "\n",
            stderr="",
        )
        with (
            patch(
                "omo_manager.omo_repository_custody.os.stat",
                side_effect=(socket_identity, FileNotFoundError()),
            ),
            patch("omo_manager.omo_repository_custody.subprocess.run", return_value=completed),
        ):
            with self.assertRaisesRegex(CustodyError, "socket disappeared"):
                _ = target_identity("unit:1")

    def test_prepare_disables_repository_fsmonitor_command(self) -> None:
        marker = self.base / "fsmonitor-ran"
        monitor = self.base / "fsmonitor"
        monitor.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        monitor.chmod(0o755)
        subprocess.run(
            ["git", "-C", self.repo, "config", "core.fsmonitor", str(monitor)],
            check=True,
        )
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            _ = prepare(self.args())
        self.assertFalse(marker.exists())

    def test_review_from_source_pane_with_different_task_fails(self) -> None:
        binding = self.prepare_binding()
        review = self.private / "same-pane-review.json"
        self.write_report(
            review,
            "old:1",
            self.tasks / "independent-name.md",
            canonical_json(
                {
                    "schema": REVIEW_SCHEMA,
                    "verdict": "PASS",
                    "binding_sha256": sha(binding.read_bytes()),
                    "notes": "not actually independent",
                }
            ).decode(),
        )
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "not independent"):
                execute(binding, review, state_root=self.state)

    def test_new_state_root_fsyncs_parent_and_recovers(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)
        state = self.private / "durable-state"
        synced_inodes: list[int] = []
        real_fsync = os.fsync

        def capture_fsync(descriptor: int) -> None:
            synced_inodes.append(os.fstat(descriptor).st_ino)
            real_fsync(descriptor)

        with (
            patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target),
            patch("omo_manager.omo_repository_custody.os.fsync", side_effect=capture_fsync),
        ):
            with self.assertRaisesRegex(CustodyError, "injected crash"):
                execute(binding, review, state_root=state, crash_after="prepared")
            first = execute(binding, review, state_root=state)
            self.assertEqual(first, execute(binding, review, state_root=state))
        self.assertIn(self.private.stat().st_ino, synced_inodes)

    def test_reused_binding_and_conflicting_registry_fail(self) -> None:
        binding = self.prepare_binding()
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "reused output"):
                prepare(self.args())
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        key = transaction_key(parsed)
        journal = self.state / f"{key}.prepared.json"
        journal.write_text("foreign")
        journal.chmod(0o600)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "indeterminate"):
                execute(binding, self.review(binding), state_root=self.state)

    def test_foreign_empty_source_key_directory_is_indeterminate(self) -> None:
        binding = self.prepare_binding()
        parsed: dict[str, object] = json.loads(binding.read_bytes())
        (self.state / transaction_key(parsed)).mkdir(mode=0o700)
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with self.assertRaisesRegex(CustodyError, "foreign source-key namespace"):
                execute(binding, self.review(binding), state_root=self.state)

    def test_conflicting_claimed_owner_and_destination_cannot_reuse_source_registry(self) -> None:
        binding = self.prepare_binding()
        _ = self.execute_binding(binding)
        second_task = self.tasks / "destination-two.md"
        self.write_task(second_task, "new:2", "long_running")
        replacement_todo = self.tasks / "replacement-TODO.md"
        replacement_todo.write_text("source.md old:1\ndestination.md new:1\ndestination-two.md new:2\n")
        os.replace(replacement_todo, self.todo)
        second_acceptance = self.private / "acceptance-two.json"
        second_acceptance.write_bytes(
            canonical_json(
                {
                    "schema": ACCEPTANCE_SCHEMA,
                    "accepted": True,
                    "source_owner": "alternate-source-owner",
                    "destination_owner": "destination-two",
                    "source_task": str(self.source_task.resolve()),
                    "destination_task": str(second_task.resolve()),
                    "source_target": "old:1",
                    "destination_target": "new:2",
                    "repository": str(self.repo.resolve()),
                    "todo": {
                        "path": str(self.todo.resolve()),
                        "sha256": sha(self.todo.read_bytes()),
                        "source_row": "source.md old:1",
                        "destination_row": "destination-two.md new:2",
                    },
                    "files": [
                        {"path": path, "sha256": sha((self.repo / path).read_bytes())}
                        for path in self.sources
                    ],
                    "source_receipts": [
                        {
                            "path": str(self.receipt.resolve()),
                            "sha256": sha(self.receipt.read_bytes()),
                            "producer_target": "old:1",
                            "source_task": str(self.source_task),
                        }
                    ],
                }
            )
        )
        second_acceptance.chmod(0o600)
        second_binding = self.private / "binding-two.json"
        args = replace(
            self.args(second_binding),
            source_owner="alternate-source-owner",
            destination_owner="destination-two",
            destination_task=second_task,
            destination_task_sha256=sha(second_task.read_bytes()),
            destination_target="new:2",
            acceptance_file=second_acceptance,
            acceptance_sha256=sha(second_acceptance.read_bytes()),
        )
        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            _ = prepare(args)
            with self.assertRaisesRegex(CustodyError, "indeterminate"):
                execute(second_binding, self.review(second_binding), state_root=self.state)

    def test_concurrent_identical_execution_converges(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)
        barrier = threading.Barrier(2)

        def run() -> str:
            _ = barrier.wait(timeout=3)
            return execute(binding, review, state_root=self.state)

        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(lambda _: run(), range(2)))
        self.assertEqual(results[0], results[1])

    def test_concurrent_identical_execution_creates_state_root_once(self) -> None:
        binding = self.prepare_binding()
        review = self.review(binding)
        state = self.private / "new-state"
        barrier = threading.Barrier(2)

        def run() -> str:
            _ = barrier.wait(timeout=3)
            return execute(binding, review, state_root=state)

        with patch("omo_manager.omo_repository_custody.target_identity", side_effect=self.target):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(lambda _: run(), range(2)))
        self.assertEqual(results[0], results[1])
