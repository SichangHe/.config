from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_experiment_record
from omo_manager.omo_experiment_record import RecordError, main


class ExperimentRecordTests(unittest.TestCase):
    def run_record(
        self,
        output: Path,
        transcript: Path,
        prompt: Path,
        *inputs: Path,
        started_at: str = "2026-08-13T12:00:00-07:00",
        ended_at: str = "2026-08-13T20:30:00Z",
    ) -> tuple[int, str, str]:
        argv = [
            "--output-dir",
            str(output),
            "--transcript",
            str(transcript),
            "--prompt",
            str(prompt),
            "--started-at",
            started_at,
            "--ended-at",
            ended_at,
        ]
        for attachment in inputs:
            argv.extend(("--input", str(attachment)))
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def make_sources(self, root: Path) -> tuple[Path, Path]:
        transcript = root / "turns.jsonl"
        prompt = root / "prompt.txt"
        _ = transcript.write_bytes(b"agent turn one\x00\nagent turn two\n")
        _ = prompt.write_bytes(b"prompt bytes\xff\n")
        return transcript, prompt

    def test_preserves_exact_copies_hashes_elapsed_scope_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            attachment = root / "given.bin"
            _ = attachment.write_bytes(b"\x00\x01input\xfe")
            output = root / "record"

            status, stdout, stderr = self.run_record(output, transcript, prompt, attachment)

            self.assertEqual(0, status, stderr)
            self.assertEqual(f"{output}\n", stdout)
            destinations = {
                "transcript": output / "transcripts/turns.jsonl",
                "prompt": output / "attachments/prompt/prompt.txt",
                "input": output / "attachments/inputs/given.bin",
            }
            sources = {"transcript": transcript, "prompt": prompt, "input": attachment}
            manifest = json.loads((output / "manifest.json").read_bytes())
            self.assertEqual("omo-experiment-record/v1", manifest["schema"])
            self.assertEqual(5400.0, manifest["timing"]["elapsed_seconds"])
            entries = {entry["role"]: entry for entry in manifest["files"]}
            for role, destination in destinations.items():
                source_bytes = sources[role].read_bytes()
                self.assertEqual(source_bytes, destination.read_bytes())
                expected_hash = hashlib.sha256(source_bytes).hexdigest()
                self.assertEqual(expected_hash, entries[role]["source"]["sha256"])
                self.assertEqual(expected_hash, entries[role]["destination"]["sha256"])
                self.assertEqual(len(source_bytes), entries[role]["source"]["size_bytes"])
                self.assertEqual(len(source_bytes), entries[role]["destination"]["size_bytes"])
            summary = (output / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("preserves only explicitly supplied files", summary)
            self.assertIn("does not establish global transcript completeness", summary)
            for path in output.rglob("*"):
                if path.is_file():
                    self.assertEqual(0, stat.S_IMODE(path.stat().st_mode) & 0o111)

    def test_extracts_representative_codex_cumulative_token_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "codex.jsonl"
            prompt = root / "prompt.txt"
            records = [
                {"timestamp": "2026-08-13T19:00:00Z", "type": "session_meta", "payload": {"id": "session-1"}},
                {
                    "timestamp": "2026-08-13T19:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 4}}},
                },
                {
                    "timestamp": "2026-08-13T19:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 175, "cached_input_tokens": 40, "output_tokens": 35, "reasoning_output_tokens": 12}}},
                },
            ]
            _ = transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            _ = prompt.write_text("prompt\n", encoding="utf-8")
            output = root / "record"

            status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertEqual(0, status, stderr)
            usage = json.loads((output / "manifest.json").read_bytes())["token_usage"]
            self.assertEqual("available", usage["status"])
            self.assertEqual(175, usage["input_tokens"])
            self.assertEqual(40, usage["cached_input_tokens"])
            self.assertEqual(35, usage["output_tokens"])
            self.assertEqual(12, usage["reasoning_output_tokens"])
            self.assertEqual(210, usage["total_tokens"])

    def test_reports_unavailable_tokens_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            output = root / "record"

            status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertEqual(0, status, stderr)
            manifest = json.loads((output / "manifest.json").read_bytes())
            self.assertEqual("unavailable", manifest["token_usage"]["status"])
            self.assertNotIn("total_tokens", manifest["token_usage"])
            self.assertIn("token usage: unavailable", (output / "summary.txt").read_text(encoding="utf-8"))

    def test_ambiguous_codex_token_timestamp_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "codex.jsonl"
            prompt = root / "prompt.txt"
            records = [
                {
                    "timestamp": "2026-08-13T19:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 4}}},
                },
                {
                    "timestamp": "not-a-timestamp",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 200, "cached_input_tokens": 40, "output_tokens": 20, "reasoning_output_tokens": 8}}},
                },
            ]
            _ = transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            _ = prompt.write_text("prompt\n", encoding="utf-8")
            output = root / "record"

            status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertEqual(0, status, stderr)
            usage = json.loads((output / "manifest.json").read_bytes())["token_usage"]
            self.assertEqual("unavailable", usage["status"])
            self.assertIn("timezone-aware", usage["reason"])
            self.assertNotIn("total_tokens", usage)

    def test_rejects_missing_timezone_and_negative_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            naive_output = root / "naive"
            negative_output = root / "negative"

            naive_status, _, naive_error = self.run_record(naive_output, transcript, prompt, started_at="2026-08-13T12:00:00")
            negative_status, _, negative_error = self.run_record(
                negative_output,
                transcript,
                prompt,
                started_at="2026-08-13T12:00:00Z",
                ended_at="2026-08-13T11:59:59Z",
            )

            self.assertNotEqual(0, naive_status)
            self.assertIn("explicit timezone", naive_error)
            self.assertFalse(naive_output.exists())
            self.assertNotEqual(0, negative_status)
            self.assertIn("must not be earlier", negative_error)
            self.assertFalse(negative_output.exists())

    def test_rejects_duplicate_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_dir = root / "first"
            second_dir = root / "second"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / "turns.jsonl"
            second = second_dir / "turns.jsonl"
            prompt = root / "prompt.txt"
            _ = first.write_text("one\n", encoding="utf-8")
            _ = second.write_text("two\n", encoding="utf-8")
            _ = prompt.write_text("prompt\n", encoding="utf-8")
            output = root / "record"

            stderr = StringIO()
            with redirect_stderr(stderr):
                status = main(
                    [
                        "--output-dir",
                        str(output),
                        "--transcript",
                        str(first),
                        "--transcript",
                        str(second),
                        "--prompt",
                        str(prompt),
                        "--started-at",
                        "2026-08-13T12:00:00Z",
                        "--ended-at",
                        "2026-08-13T12:00:01Z",
                    ]
                )

            self.assertNotEqual(0, status)
            self.assertIn("duplicate supplied basename", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_never_overwrites_an_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            output = root / "record"
            output.mkdir()
            sentinel = output / "keep.txt"
            _ = sentinel.write_text("keep\n", encoding="utf-8")

            status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertNotEqual(0, status)
            self.assertIn("already exists", stderr)
            self.assertEqual("keep\n", sentinel.read_text(encoding="utf-8"))

    def test_rejects_symlinked_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            real_parent = root / "real"
            real_parent.mkdir()
            symlinked_parent = root / "link"
            symlinked_parent.symlink_to(real_parent, target_is_directory=True)

            status, _, stderr = self.run_record(symlinked_parent / "record", transcript, prompt)

            self.assertNotEqual(0, status)
            self.assertIn("no symlink components", stderr)
            self.assertFalse((real_parent / "record").exists())

    def test_rejects_output_parent_available_to_other_users(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            shared_parent = root / "shared"
            shared_parent.mkdir(mode=0o733)
            shared_parent.chmod(0o733)

            status, _, stderr = self.run_record(shared_parent / "record", transcript, prompt)

            self.assertNotEqual(0, status)
            self.assertIn("grant no group or other write permissions", stderr)
            self.assertFalse((shared_parent / "record").exists())

    def test_output_parent_swap_cannot_redirect_staging_or_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            original_parent = root / "original"
            (original_parent / "records").mkdir(parents=True)
            (original_parent / "records").chmod(0o700)
            redirect_parent = root / "redirect"
            (redirect_parent / "records").mkdir(parents=True)
            moved_parent = root / "moved"
            output = original_parent / "records/record"
            original_source_specs = omo_experiment_record.source_specs

            def swap_parent(args: omo_experiment_record.Args) -> tuple[omo_experiment_record.SourceSpec, ...]:
                original_parent.rename(moved_parent)
                original_parent.symlink_to(redirect_parent, target_is_directory=True)
                return original_source_specs(args)

            with patch.object(omo_experiment_record, "source_specs", side_effect=swap_parent):
                status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertNotEqual(0, status)
            self.assertIn("no symlink components", stderr)
            self.assertFalse((redirect_parent / "records/record").exists())
            self.assertFalse((moved_parent / "records/record").exists())
            self.assertEqual([], list((moved_parent / "records").glob(".record.staging-*")))

    def test_parent_swap_just_after_revalidation_rolls_back_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            original_parent = root / "original"
            (original_parent / "records").mkdir(parents=True)
            (original_parent / "records").chmod(0o700)
            redirect_parent = root / "redirect"
            (redirect_parent / "records").mkdir(parents=True)
            moved_parent = root / "moved"
            output = original_parent / "records/record"
            original_revalidate = omo_experiment_record.revalidate_output_parent
            swapped = False

            def revalidate_then_swap(target: omo_experiment_record.OutputTarget) -> None:
                nonlocal swapped
                original_revalidate(target)
                if not swapped:
                    swapped = True
                    original_parent.rename(moved_parent)
                    original_parent.symlink_to(redirect_parent, target_is_directory=True)

            with patch.object(omo_experiment_record, "revalidate_output_parent", side_effect=revalidate_then_swap):
                status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertNotEqual(0, status)
            self.assertIn("no symlink components", stderr)
            self.assertFalse((redirect_parent / "records/record").exists())
            self.assertFalse((moved_parent / "records/record").exists())
            self.assertEqual([], list((moved_parent / "records").glob(".record.staging-*")))

    def test_syncs_staged_directories_before_the_published_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            output = root / "record"

            with patch.object(
                omo_experiment_record,
                "fsync_directory_descriptor",
                wraps=omo_experiment_record.fsync_directory_descriptor,
            ) as fsync_directory:
                status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertEqual(0, status, stderr)
            labels = [call.args[1] for call in fsync_directory.call_args_list]
            self.assertTrue(any(label.startswith("staged record directory") for label in labels))
            self.assertEqual("output parent directory", labels[-1])

    def test_validation_failure_leaves_no_accepted_or_staged_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, prompt = self.make_sources(root)
            output = root / "record"

            with patch("omo_manager.omo_experiment_record.validate_staged_record", side_effect=RecordError("validation failed")):
                status, _, stderr = self.run_record(output, transcript, prompt)

            self.assertNotEqual(0, status)
            self.assertIn("validation failed", stderr)
            self.assertFalse(output.exists())
            self.assertEqual([], list(root.glob(".record.staging-*")))

    def test_executable_wrapper_exposes_help(self) -> None:
        wrapper = Path(__file__).resolve().parents[2] / "bin/omo_experiment_record.py"
        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        result = subprocess.run([str(wrapper), "--help"], capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--transcript", result.stdout)
        self.assertIn("--prompt", result.stdout)
        self.assertNotIn("launch", result.stdout.lower())


if __name__ == "__main__":
    _ = unittest.main()
