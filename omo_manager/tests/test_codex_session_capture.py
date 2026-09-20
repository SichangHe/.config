import tempfile
import unittest
import re
from argparse import Namespace
from contextlib import contextmanager, nullcontext
from pathlib import Path

from unittest.mock import ANY, patch
from omo_manager.omo_codex_start import Args, Pane, PreparedPrompt, PromptHandoffError, StartError, exact_retained_status_action, exact_status_probe_rendering, exact_status_probe_suffix_at_cursor, launch_command, prompt_rendering_matches_bound_source, query_exact_status_session_id, record_session_id, send_prompt, verify_prompt_submitted
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_codex_session_migrate import CODEX_PANE_COMMANDS, active_target_owners, authorized_human_targets, candidates, parse_args as parse_migration_args, run as run_migration, selected_candidates, submit_existing_input


def accepted_tmux_marker(command: str) -> str:
    markers = re.findall(r"display-message -p (OMO_[A-Za-z0-9_-]+)", command)
    return next(marker for marker in markers if "REJECT" not in marker)


class CodexSessionCaptureTests(unittest.TestCase):
    UUID = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"
    OLD_UUID = "019f670b-6a2f-7463-b9be-9aa6ff0cec42"
    PID = __import__("os").getpid()
    READY = ["› Use /skills to list available skills", "  gpt-5.6-terra high · /tmp · Context 0% used"]

    def test_collapsed_prompt_is_never_full_source_proof(self):
        source = "exact frozen source\n"
        prepared = PreparedPrompt(
            Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246),
            "buffer",
            "condition",
            "nonce",
            source,
            __import__("hashlib").sha256(source.encode()).hexdigest(),
            "probe",
        )
        self.assertFalse(prompt_rendering_matches_bound_source(f"[Pasted Content {len(source)} chars]", prepared))
        self.assertFalse(prompt_rendering_matches_bound_source("[Pasted Content 999 chars]", prepared))
        self.assertFalse(prompt_rendering_matches_bound_source("[Pasted text #1 +12 lines]", prepared))

    def test_prompt_submission_deadline_rejects_retained_composer(self) -> None:
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246, 7001)
        source = "exact retained task\n"
        prepared = PreparedPrompt(
            pane,
            "buffer",
            "condition",
            "nonce",
            source,
            __import__("hashlib").sha256(source.encode()).hexdigest(),
            "probe",
        )
        with (
            patch("omo_manager.omo_codex_start.prompt_state", return_value=("running", [], "exact retained task")),
            patch("omo_manager.omo_codex_start.guarded_prompt_enter") as enter,
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=[0.0, 0.0, 46.0]),
            patch("omo_manager.omo_codex_start.time.sleep"),
            self.assertRaisesRegex(PromptHandoffError, "remained in the composer after the delivery deadline"),
        ):
            verify_prompt_submitted(prepared)

        enter.assert_called_once_with(pane, "condition", "nonce", 1)

    def test_prompt_submission_never_retries_unbound_composer_text(self) -> None:
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246, 7001)
        source = "exact retained task\n"
        prepared = PreparedPrompt(
            pane,
            "buffer",
            "condition",
            "nonce",
            source,
            __import__("hashlib").sha256(source.encode()).hexdigest(),
            "probe",
        )
        with (
            patch("omo_manager.omo_codex_start.prompt_state", return_value=("running", [], "unrelated stale input")),
            patch("omo_manager.omo_codex_start.guarded_prompt_enter") as enter,
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=[0.0, 0.0]),
            self.assertRaisesRegex(PromptHandoffError, "no longer matches the frozen source"),
        ):
            verify_prompt_submitted(prepared)

        enter.assert_not_called()

    def test_fresh_prompt_rejects_buffer_drift_before_paste(self):
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246)
        calls: list[list[str]] = []
        saves = iter(("exact\n", "changed\n"))

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "save-buffer"]:
                return __import__("subprocess").CompletedProcess(argv, 0, next(saves), "")
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt"
            prompt.write_text("exact\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_start.run", side_effect=run),
                patch("omo_manager.omo_codex_start.verify_same_process"),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.READY)),
                self.assertRaisesRegex(StartError, "buffer does not equal"),
            ):
                send_prompt(pane, prompt)

        self.assertFalse(any(call[:3] == ["tmux", "if-shell", "-F"] for call in calls))

    def stale_fixture(self, directory: str, body: str = ""):
        root = Path(directory)
        text = f"---\nversion: v1.0.0\nstatus: running\nrunat: w:2\ntool: codex\nsession_id: {self.OLD_UUID}\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n{body}"
        task = root / "worker.md"
        task.write_text(text, encoding="utf-8")
        pane = Pane("w:2", "%2", "@2", "bunx", root, self.PID, 100)
        report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("ready", [])
        args = Namespace(root=root, apply=True, task="worker.md", task_sha256=__import__("hashlib").sha256(task.read_bytes()).hexdigest(), expected_old_session_id=self.OLD_UUID)
        return root, task, text, pane, report, args

    def test_fresh_prompt_rejects_and_contains_collapsed_composer(self):
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246)
        ready = ["› Use /skills to list available skills", "  gpt-5.6-terra high · /tmp · Context 0% used"]
        probe = "__OMO_PROMPT_END_123-7__"
        pasted = [f"› [Pasted Content 2041 chars]{probe}", "  gpt-5.6-terra high · /tmp · Context 0% used"]
        calls: list[list[str]] = []
        loaded_sources: list[bytes] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "load-buffer"]:
                loaded_sources.append(Path(argv[-1]).read_bytes())
            if argv[:2] == ["tmux", "save-buffer"]:
                return __import__("subprocess").CompletedProcess(argv, 0, prompt.read_bytes().decode("utf-8"), "")
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                accepted = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, accepted + "\n", "")
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt"
            prompt.write_bytes(b"x" * 2039 + b"\r\n")
            with (
                patch("omo_manager.omo_codex_start.run", side_effect=run),
                patch("omo_manager.omo_codex_start.verify_same_process"),
                patch(
                    "omo_manager.omo_codex_start.exact_tail",
                    side_effect=((True, ready), (True, ready), (True, pasted)),
                ),
                patch("omo_manager.omo_codex_start.stop_unverified_replacement", return_value=Pane("dw:46.0", "%46", "@46", "zsh", Path("/tmp"), 5252)) as contain,
                patch("omo_manager.omo_codex_start.os.getpid", return_value=123),
                patch("omo_manager.omo_codex_start.time.monotonic_ns", return_value=7),
                patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 6.0)),
                self.assertRaisesRegex(StartError, "paste was not proven complete"),
            ):
                send_prompt(pane, prompt)

        self.assertEqual([b"x" * 2039 + b"\r\n"], loaded_sources)
        guarded = [call for call in calls if call[:3] == ["tmux", "if-shell", "-F"]]
        self.assertEqual(1, len(guarded))
        self.assertNotIn(" Enter", guarded[0][6])
        self.assertIn(probe, guarded[0][6])
        self.assertTrue(all("#{pane_id},%46" in call[5] for call in guarded))
        self.assertTrue(all("#{pane_pid},4246" in call[5] for call in guarded))
        self.assertEqual(1, sum(call[:3] == ["tmux", "delete-buffer", "-b"] for call in calls))
        contain.assert_called_once()

    def test_fresh_prompt_does_not_retry_plan_prompt(self):
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246)
        plan = ["Create a plan? shift + tab use Plan mode esc dismiss", "› choose an option", "  gpt-5.6-terra high"]
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "save-buffer"]:
                return __import__("subprocess").CompletedProcess(argv, 0, prompt.read_bytes().decode("utf-8"), "")
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                accepted = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, accepted + "\n", "")
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt"
            prompt.write_text("work", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_start.run", side_effect=run),
                patch("omo_manager.omo_codex_start.verify_same_process"),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, plan)),
                patch("omo_manager.omo_codex_start.time.monotonic", return_value=0.0),
                self.assertRaisesRegex(StartError, "unsafe Plan prompt"),
            ):
                send_prompt(pane, prompt)

        guarded = [call for call in calls if call[:3] == ["tmux", "if-shell", "-F"]]
        self.assertEqual(0, len(guarded))

    def test_fresh_prompt_stops_without_retry_after_process_identity_change(self):
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246)
        ready = ["› Use /skills to list available skills", "  gpt-5.6-terra high"]
        collapsed = ["› [Pasted Content 2041 chars]", "  gpt-5.6-terra high"]
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "save-buffer"]:
                return __import__("subprocess").CompletedProcess(argv, 0, prompt.read_bytes().decode("utf-8"), "")
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                accepted = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, accepted + "\n", "")
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt"
            prompt.write_text("work", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_start.run", side_effect=run),
                patch(
                    "omo_manager.omo_codex_start.verify_same_process",
                    side_effect=(*((None,) * 7), StartError("tmux pane process identity changed before process replacement.")),
                ),
                patch("omo_manager.omo_codex_start.stop_unverified_replacement", return_value=Pane("dw:46.0", "%46", "@46", "zsh", Path("/tmp"), 5252)) as contain,
                patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, ready), (True, ready), (True, collapsed))),
                self.assertRaisesRegex(StartError, "process identity changed"),
            ):
                send_prompt(pane, prompt)

        guarded = [call for call in calls if call[:3] == ["tmux", "if-shell", "-F"]]
        self.assertEqual(1, len(guarded))
        contain.assert_called_once()

    def test_fresh_prompt_stops_without_retry_after_ui_change(self):
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246)
        ready = ["› Use /skills to list available skills", "  gpt-5.6-terra high"]
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "save-buffer"]:
                return __import__("subprocess").CompletedProcess(argv, 0, prompt.read_bytes().decode("utf-8"), "")
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                accepted = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, accepted + "\n", "")
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt"
            prompt.write_text("work", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_start.run", side_effect=run),
                patch("omo_manager.omo_codex_start.verify_same_process"),
                patch("omo_manager.omo_codex_start.stop_unverified_replacement", return_value=Pane("dw:46.0", "%46", "@46", "zsh", Path("/tmp"), 5252)) as contain,
                patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, ready), (True, ready), (True, ["shell prompt"]))),
                self.assertRaisesRegex(StartError, "no longer sees a Codex interface"),
            ):
                send_prompt(pane, prompt)

        guarded = [call for call in calls if call[:3] == ["tmux", "if-shell", "-F"]]
        self.assertEqual(1, len(guarded))
        contain.assert_called_once()

    def test_fresh_prompt_stops_without_retry_after_error(self):
        pane = Pane("dw:46.0", "%46", "@46", "bunx", Path("/tmp"), 4246)
        ready = ["› Use /skills to list available skills", "  gpt-5.6-terra high"]
        error = ["■ Error: 429 Too Many Requests", "› [Pasted Content 2041 chars]", "  gpt-5.6-terra high"]
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "save-buffer"]:
                return __import__("subprocess").CompletedProcess(argv, 0, prompt.read_bytes().decode("utf-8"), "")
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                accepted = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, accepted + "\n", "")
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt"
            prompt.write_text("work", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_start.run", side_effect=run),
                patch("omo_manager.omo_codex_start.verify_same_process"),
                patch("omo_manager.omo_codex_start.stop_unverified_replacement", return_value=Pane("dw:46.0", "%46", "@46", "zsh", Path("/tmp"), 5252)) as contain,
                patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, ready), (True, ready), (True, error))),
                self.assertRaisesRegex(StartError, "showed an error"),
            ):
                send_prompt(pane, prompt)

        guarded = [call for call in calls if call[:3] == ["tmux", "if-shell", "-F"]]
        self.assertEqual(1, len(guarded))
        contain.assert_called_once()

    def test_migration_accepts_live_bunx_launcher_command(self):
        self.assertIn("bunx", CODEX_PANE_COMMANDS)

    def test_existing_input_enter_is_exact_process_guarded(self):
        pane = Pane("w:1.0", "%1", "@1", "bunx", Path("/tmp"), 42, 7001)

        def run(argv, **_kwargs):
            accepted = accepted_tmux_marker(argv[6])
            return __import__("subprocess").CompletedProcess(argv, 0, accepted + "\n", "")

        with patch("omo_manager.omo_codex_session_migrate.subprocess.run", side_effect=run) as mocked:
            submit_existing_input(pane)
        argv = mocked.call_args.args[0]
        self.assertIn("#{pane_id},%1", argv[5])
        self.assertIn("#{window_id},@1", argv[5])
        self.assertIn("#{session_name}:#{window_index}.#{pane_index},w:1.0", argv[5])
        self.assertIn("#{pane_pid},42", argv[5])
        self.assertIn("#{pane_current_command},bunx", argv[5])
        self.assertIn("/proc/42/stat", argv[6])
        self.assertIn('test "${20:-}" = 7001', argv[6])
        self.assertEqual(1, argv[6].count(" Enter"))

    def test_human_owned_candidate_requires_explicit_option(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: hcfg:1\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "human.md"
            task.write_text(text)
            pane = Pane("hcfg:1", "%1", "@1", "bunx", root, 41)
            report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("ready", [])
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report):
                self.assertEqual([], candidates(root))
                self.assertEqual([task], candidates(root, {"hcfg:1"}))

    def test_human_owned_apply_records_with_explicit_option(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: hcfg:1\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "human.md"
            task.write_text(text)
            pane = Pane("hcfg:1", "%1", "@1", "bunx", root, 41)
            report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("ready", [])
            with patch("omo_manager.omo_codex_session_migrate.authorized_human_targets", return_value={"hcfg:1"}), patch("omo_manager.omo_codex_session_migrate.candidates", return_value=[task]), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", return_value=self.UUID), patch("omo_manager.omo_codex_session_migrate.record_session_id") as record:
                self.assertEqual(0, run_migration(Namespace(root=root, apply=True, include_human_owned=True)))
            record.assert_called_once_with(task, self.UUID, ANY, lock_held=True)

    def test_human_authority_must_name_each_exact_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mail = root / "manager_mail"
            mail.mkdir(mode=0o700)
            source = mail / "request.txt"
            text = "Subject: status\n\nDo these to the human windows as well: hcfg:1.\n"
            source.write_text(text, encoding="utf-8")
            source.chmod(0o600)
            base = Namespace(
                include_human_owned=True,
                human_target=["hcfg:1"],
                human_authority_file=Path("manager_mail/request.txt"),
                human_authority_lines=(3, 3),
                human_authority_sha256=__import__("hashlib").sha256(text.encode()).hexdigest(),
            )
            self.assertEqual({"hcfg:1"}, authorized_human_targets(base, root))
            base.human_target = ["hcfg:2"]
            with self.assertRaisesRegex(ValueError, "does not name"):
                authorized_human_targets(base, root)
            source.write_text(text.replace("hcfg:1", "hcfg:10"), encoding="utf-8")
            base.human_target = ["hcfg:1"]
            base.human_authority_sha256 = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "does not name"):
                authorized_human_targets(base, root)

    def test_migration_failure_is_per_candidate_skip(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: {target}\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "one.md", root / "two.md"]
            for index, path in enumerate(paths, start=1):
                path.write_text(text.format(target=f"w:{index}"))

            def pane(target: str) -> Pane:
                return Pane(target, f"%{target[-1]}", f"@{target[-1]}", "bunx", root, 40 + int(target[-1]))

            report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("ready", [])
            with patch("omo_manager.omo_codex_session_migrate.candidates", return_value=paths), patch("omo_manager.omo_codex_session_migrate.resolve_pane", side_effect=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", side_effect=(RuntimeError("capture failed"), self.UUID)), patch("omo_manager.omo_codex_session_migrate.record_session_id") as record:
                self.assertEqual(0, run_migration(Namespace(root=root, apply=True)))
            record.assert_called_once()
            self.assertEqual(paths[1], record.call_args.args[0])

    def test_exact_task_selector_returns_only_named_eligible_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "one.md", root / "nested" / "two.md"]
            paths[1].parent.mkdir()
            paths[1].write_text("task")
            task_sha256 = __import__("hashlib").sha256(b"task").hexdigest()
            metadata = __import__("omo_manager.omo_task_metadata", fromlist=["TaskMetadata"])
            parsed = metadata.TaskMetadata("v1.0.0", "running", "w:2", "codex", "mgr:1", False, ())
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=tuple(paths)), patch("omo_manager.omo_codex_session_migrate.parse_task_metadata", return_value=parsed), patch("omo_manager.omo_codex_session_migrate.active_target_owners", return_value=(paths[1].resolve(),)), patch("omo_manager.omo_codex_session_migrate.candidates", return_value=[paths[1]]) as candidate_scan:
                self.assertEqual([paths[1]], selected_candidates(root, set(), "nested/two.md", task_sha256))
            candidate_scan.assert_called_once_with(root, set(), (paths[1],))

    def test_exact_task_selector_ignores_unrelated_markdown_outside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "work_logs"
            root.mkdir()
            task = root / "selected.md"
            task.write_text(
                "---\nversion: v1.0.0\nstatus: running\nrunat: w:2\ntool: codex\nmanagerat: mgr:1\nis_manager: false\npending_task_items: []\n---\n"
            )
            outside = base / "unrelated.md"
            outside.write_text(
                "---\nversion: v1.0.0\nstatus: running\nrunat: other:9\ntool: codex\nmanagerat: mgr:1\nis_manager: false\npending_task_items: []\n---\n"
            )
            task_sha256 = __import__("hashlib").sha256(task.read_bytes()).hexdigest()
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(outside, task)), patch("omo_manager.omo_codex_session_migrate.candidates", return_value=[task]) as candidate_scan:
                self.assertEqual([task], selected_candidates(root, set(), "selected.md", task_sha256))
            candidate_scan.assert_called_once_with(root, set(), (task,))

    def test_exact_task_selector_rejects_external_same_target_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "work_logs"
            root.mkdir()
            text = "---\nversion: v1.0.0\nstatus: running\nrunat: w:2\ntool: codex\nmanagerat: mgr:1\nis_manager: false\npending_task_items: []\n---\n"
            task = root / "selected.md"
            task.write_text(text)
            outside = base / "other-owner.md"
            outside.write_text(text)
            task_sha256 = __import__("hashlib").sha256(task.read_bytes()).hexdigest()
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(outside, task)):
                with self.assertRaisesRegex(ValueError, "exactly one active owner"):
                    selected_candidates(root, set(), "selected.md", task_sha256)

    def test_exact_task_selector_rejects_missing_or_non_normalized_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "one.md"
            task.write_text("task")
            task_sha256 = __import__("hashlib").sha256(b"task").hexdigest()
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)):
                with self.assertRaisesRegex(ValueError, "not exactly one eligible"):
                    selected_candidates(root, set(), "missing.md", task_sha256)
                for invalid in ("/one.md", "../one.md", "nested/../one.md"):
                    with self.assertRaisesRegex(ValueError, "normalized root-relative POSIX"):
                        selected_candidates(root, set(), invalid, task_sha256)

    def test_exact_task_selector_requires_and_revalidates_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "one.md"
            task.write_text("task")
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.candidates") as candidate_scan:
                with self.assertRaisesRegex(ValueError, "not exactly one eligible"):
                    selected_candidates(root, set(), "one.md")
                with self.assertRaisesRegex(ValueError, "digest changed"):
                    selected_candidates(root, set(), "one.md", "0" * 64)
            candidate_scan.assert_not_called()

    def test_exact_task_selector_rejects_same_target_ambiguity_in_any_order(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: {target}\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "a.md", root / "b.md"]
            paths[0].write_text(text.format(target="w:1"))
            paths[1].write_text(text.format(target="w:01.0"))
            task_sha256 = __import__("hashlib").sha256(paths[1].read_bytes()).hexdigest()
            for order in (tuple(paths), tuple(reversed(paths))):
                with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=order):
                    self.assertEqual(tuple(sorted(path.resolve() for path in paths)), active_target_owners(root, "w:1"))
                    with self.assertRaisesRegex(ValueError, "exactly one active owner"):
                        selected_candidates(root, set(), "b.md", task_sha256)

    def test_exact_task_options_are_paired(self):
        with self.assertRaises(SystemExit):
            parse_migration_args(["--root", "/tmp", "--task", "one.md"])
        with self.assertRaises(SystemExit):
            parse_migration_args(["--root", "/tmp", "--task-sha256", "0" * 64])

    def test_stale_session_assertion_requires_exact_task_binding(self):
        with self.assertRaises(SystemExit):
            parse_migration_args(["--root", "/tmp", "--expected-old-session-id", self.OLD_UUID])
        with self.assertRaises(SystemExit):
            parse_migration_args(["--root", "/tmp", "--task", "one.md", "--task-sha256", "0" * 64, "--expected-old-session-id", self.OLD_UUID])
        with self.assertRaises(SystemExit):
            parse_migration_args(["--root", "/tmp", "--apply", "--task", "one.md", "--task-sha256", "0" * 64, "--expected-old-session-id", "invalid"])
        parsed = parse_migration_args(["--root", "/tmp", "--apply", "--task", "one.md", "--task-sha256", "0" * 64, "--expected-old-session-id", self.OLD_UUID])
        self.assertEqual(self.OLD_UUID, parsed.expected_old_session_id)

    def test_stale_session_repair_binds_independently_queried_live_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, task, text, pane, report, args = self.stale_fixture(directory, "Exact body bytes.\n")
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", return_value=self.UUID) as query:
                self.assertEqual(0, run_migration(args))
            query.assert_called_once_with(pane, 240, 10.0, self.OLD_UUID, require_complete_status_card=True)
            self.assertEqual(text.replace(self.OLD_UUID, self.UUID), task.read_text(encoding="utf-8"))

    def test_stale_session_repair_holds_input_lock_through_query(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, task, text, pane, report, args = self.stale_fixture(directory)
            input_locked = False

            @contextmanager
            def input_lock(_target):
                nonlocal input_locked
                input_locked = True
                try:
                    yield
                finally:
                    input_locked = False

            def query(*_args, **_kwargs):
                self.assertTrue(input_locked)
                return self.UUID

            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.tmux_input_lock", side_effect=input_lock), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", side_effect=query):
                self.assertEqual(0, run_migration(args))
            self.assertFalse(input_locked)
            self.assertEqual(text.replace(self.OLD_UUID, self.UUID), task.read_text(encoding="utf-8"))

    def test_ordinary_session_capture_holds_input_lock_through_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "---\nversion: v1.0.0\nstatus: running\nrunat: w:2\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n"
            task = root / "worker.md"
            task.write_text(text, encoding="utf-8")
            pane = Pane("w:2", "%2", "@2", "bunx", root, self.PID, 7001)
            report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("ready", [])
            args = Namespace(root=root, apply=True)
            input_locked = False

            @contextmanager
            def input_lock(target: str):
                nonlocal input_locked
                self.assertEqual("w:2", target)
                input_locked = True
                try:
                    yield
                finally:
                    input_locked = False

            def query(*_args, **_kwargs):
                self.assertTrue(input_locked)
                return self.UUID

            with patch("omo_manager.omo_codex_session_migrate.candidates", return_value=[task]), patch(
                "omo_manager.omo_codex_session_migrate.tmux_input_lock", side_effect=input_lock
            ), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch(
                "omo_manager.omo_codex_session_migrate.inspect", return_value=report
            ), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", side_effect=query):
                self.assertEqual(0, run_migration(args))
            self.assertFalse(input_locked)
            self.assertIn(f"session_id: {self.UUID}\n", task.read_text(encoding="utf-8"))

    def test_stale_session_repair_rejects_same_live_uuid_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, task, text, pane, report, args = self.stale_fixture(directory)
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", return_value=self.OLD_UUID), self.assertRaisesRegex(ValueError, "still equals"):
                run_migration(args)
            self.assertEqual(text, task.read_text(encoding="utf-8"))

    def test_stale_session_repair_never_submits_existing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, task, text, pane, report, args = self.stale_fixture(directory)
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.current_input_text", return_value="unsent work"), patch("omo_manager.omo_codex_session_migrate.submit_existing_input") as submit, patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id") as query, self.assertRaisesRegex(ValueError, "empty Codex composer"):
                run_migration(args)
            submit.assert_not_called()
            query.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))

    def test_stale_session_repair_rejects_old_uuid_mismatch_without_query(self):
        different_old_uuid = "019f670b-6a2f-7463-b9be-9aa6ff0cec41"
        with tempfile.TemporaryDirectory() as directory:
            _root, task, text, _pane, _report, args = self.stale_fixture(directory)
            args.expected_old_session_id = different_old_uuid
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id") as query, self.assertRaisesRegex(ValueError, "not exactly one eligible"):
                run_migration(args)
            query.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))

    def test_stale_session_repair_rejects_task_drift_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, task, text, pane, report, args = self.stale_fixture(directory)

            def drift(*_args, **_kwargs):
                task.write_text(text + "concurrent change\n", encoding="utf-8")
                return self.UUID

            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", side_effect=drift), self.assertRaisesRegex(StartError, "task changed before session UUID binding"):
                run_migration(args)
            self.assertEqual(text + "concurrent change\n", task.read_text(encoding="utf-8"))

    def test_stale_session_repair_rejects_session_assertion_drift(self):
        changed_uuid = "019f670b-6a2f-7463-b9be-9aa6ff0cec40"
        with tempfile.TemporaryDirectory() as directory:
            _root, task, text, pane, report, args = self.stale_fixture(directory)

            def drift(*_args, **_kwargs):
                task.write_text(text.replace(self.OLD_UUID, changed_uuid), encoding="utf-8")
                return self.UUID

            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", side_effect=drift), self.assertRaisesRegex(ValueError, "eligibility changed"):
                run_migration(args)
            self.assertEqual(text.replace(self.OLD_UUID, changed_uuid), task.read_text(encoding="utf-8"))

    def test_stale_session_repair_rejects_new_authoritative_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root, task, text, pane, report, args = self.stale_fixture(directory)

            def add_owner(*_args, **_kwargs):
                (root / "unindexed-owner.md").write_text(text.replace(self.OLD_UUID, self.UUID), encoding="utf-8")
                return self.UUID

            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", side_effect=add_owner), self.assertRaisesRegex(ValueError, "ownership changed before"):
                run_migration(args)
            self.assertEqual(text, task.read_text(encoding="utf-8"))

    def test_stale_session_repair_rejects_live_process_drift_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root, task, text, pane, report, args = self.stale_fixture(directory)
            replacement = Pane("w:2", "%2", "@2", "bunx", root, self.PID + 1_000_000)
            with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", side_effect=(pane, pane, replacement)), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", return_value=self.UUID), self.assertRaisesRegex(ValueError, "pane identity changed"):
                run_migration(args)
            self.assertEqual(text, task.read_text(encoding="utf-8"))

    def test_stale_session_repair_rejects_start_time_or_workdir_drift(self):
        for drift in ("start time", "working directory"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root, task, text, pane, report, args = self.stale_fixture(directory)
                current = Pane(
                    pane.target,
                    pane.pane_id,
                    pane.window_id,
                    pane.command,
                    root / "moved" if drift == "working directory" else root,
                    pane.pane_pid,
                    101 if drift == "start time" else pane.start_ticks,
                )
                with patch("omo_manager.omo_codex_session_migrate.task_paths", return_value=(task,)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", side_effect=(pane, pane, current)), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", return_value=self.UUID), self.assertRaisesRegex(ValueError, "complete pane identity changed"):
                    run_migration(args)
                self.assertEqual(text, task.read_text(encoding="utf-8"))

    def test_ordinary_migration_rejects_cwd_or_start_tick_drift(self):
        for drift in ("start time", "working directory"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = root / "worker.md"
                task.write_text("---\nversion: v1.0.0\nstatus: running\nrunat: w:2\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n")
                pane = Pane("w:2", "%2", "@2", "bunx", root, self.PID, 100)
                current = Pane(
                    pane.target,
                    pane.pane_id,
                    pane.window_id,
                    pane.command,
                    root / "moved" if drift == "working directory" else root,
                    pane.pane_pid,
                    101 if drift == "start time" else pane.start_ticks,
                )
                report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("ready", [])
                with patch("omo_manager.omo_codex_session_migrate.candidates", return_value=[task]), patch(
                    "omo_manager.omo_codex_session_migrate.resolve_pane", side_effect=(pane, current)
                ), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch(
                    "omo_manager.omo_codex_session_migrate.query_exact_status_session_id", return_value=self.UUID
                ), patch("omo_manager.omo_codex_session_migrate.record_session_id") as record:
                    self.assertEqual(0, run_migration(Namespace(root=root, apply=True)))
                record.assert_not_called()

    def test_exact_task_selector_limits_apply_to_named_task(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: {target}\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "one.md", root / "two.md"]
            for index, path in enumerate(paths, start=1):
                path.write_text(text.format(target=f"w:{index}"))
            pane = Pane("w:2", "%2", "@2", "bunx", root, 42)
            report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("ready", [])
            task_sha256 = __import__("hashlib").sha256(paths[1].read_bytes()).hexdigest()
            with patch("omo_manager.omo_codex_session_migrate.selected_candidates", return_value=[paths[1]]), patch("omo_manager.omo_codex_session_migrate.active_target_owners", return_value=(paths[1].resolve(),)), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", return_value=self.UUID), patch("omo_manager.omo_codex_session_migrate.record_session_id") as record:
                self.assertEqual(0, run_migration(Namespace(root=root, apply=True, task="two.md", task_sha256=task_sha256)))
            record.assert_called_once_with(paths[1], self.UUID, ANY, lock_held=True)

    def test_running_input_is_submitted_before_query_and_record(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: codex\nmanagerat: mgr:9\nis_manager: false\npending_task_items: []\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "running.md"
            task.write_text(text)
            pane = Pane("w:1", "%1", "@1", "bunx", root, 41)
            report = __import__("omo_manager.omo_codex_status", fromlist=["Report"]).Report("running", [])
            order: list[str] = []
            with patch("omo_manager.omo_codex_session_migrate.candidates", return_value=[task]), patch("omo_manager.omo_codex_session_migrate.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_session_migrate.inspect", return_value=report), patch("omo_manager.omo_codex_session_migrate.current_input_text", side_effect=("queued work", "")), patch("omo_manager.omo_codex_session_migrate.submit_existing_input", side_effect=lambda _pane: order.append("submit")), patch("omo_manager.omo_codex_session_migrate.query_exact_status_session_id", side_effect=lambda *_args: order.append("query") or self.UUID), patch("omo_manager.omo_codex_session_migrate.record_session_id", side_effect=lambda *_args, **_kwargs: order.append("record")):
                self.assertEqual(0, run_migration(Namespace(root=root, apply=True)))
            self.assertEqual(["submit", "query", "record"], order)

    def test_frontmatter_session_id_round_trip(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: codex\nmanagerat: w:2\nis_manager: false\npending_task_items: []\n---\nbody\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text(text)
            record_session_id(path, self.UUID)
            self.assertEqual(parse_task_metadata(path.read_text()).session_id, self.UUID)

    def test_frontmatter_session_id_preserves_raw_crlf_body_and_digest_binding(self):
        raw = (
            b"---\n"
            b"version: v1.0.0\n"
            b"status: long_running\n"
            b"runat: dw:59\n"
            b"tool: codex\n"
            b"managerat: wl:1\n"
            b"is_manager: true\n"
            b"pending_task_items: []\n"
            b"---\n"
            b"<human_instruction>\r\nExact Human text\r\n</human_instruction>\r\n"
        )
        expected = raw.replace(b"pending_task_items: []\n", b"pending_task_items: []\nsession_id: " + self.UUID.encode() + b"\n", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_bytes(raw)

            record_session_id(path, self.UUID, __import__("hashlib").sha256(raw).hexdigest())

            self.assertEqual(expected, path.read_bytes())
            self.assertIn(b"<human_instruction>\r\nExact Human text\r\n</human_instruction>\r\n", path.read_bytes())

    def test_rejects_digest_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text("---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: codex\nmanagerat: w:2\nis_manager: false\npending_task_items: []\n---\n")
            path.write_text(path.read_text() + "drift")
            with self.assertRaises(StartError):
                record_session_id(path, self.UUID, "0" * 64)

    def test_deliberately_fresh_launch_can_replace_stale_session_id(self):
        stale = "11111111-1111-4111-8111-111111111111"
        text = f"---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: codex\nsession_id: {stale}\nmanagerat: w:2\nis_manager: false\npending_task_items: []\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text(text)

            record_session_id(path, self.UUID, replace_existing=True)

            self.assertEqual(parse_task_metadata(path.read_text()).session_id, self.UUID)

    def test_non_codex_session_id_rejected(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: pcodx\nmanagerat: w:2\nis_manager: false\npending_task_items: []\nsession_id: %s\n---\n" % self.UUID
        with self.assertRaises(TaskFrontmatterError):
            parse_task_metadata(text)

    def test_fresh_launch_command_withholds_prompt(self):
        args = Args(Path("/tmp"), "task.md", "w:1", "gpt-5", "high", "", Path("/tmp/prompt"), 1, False, True)
        pane = Pane("w:1", "%1", "@1", "zsh", Path("/tmp"), 42)
        command = launch_command(args, pane, None, "marker")
        self.assertNotIn("prompt", command)

    def test_prompt_sender_not_called_when_capture_fails(self):
        with patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=RuntimeError("identity")) as query, patch("omo_manager.omo_codex_start.send_prompt") as prompt:
            with self.assertRaises(RuntimeError):
                query("w:1", 10, 1, tmux_guard=("w:1", "%1"))
            prompt.assert_not_called()

    def test_status_submission_is_one_exact_process_guarded_sequence(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        tails = iter(((True, ["before"]), (True, self.READY), (True, ["after"])))
        with patch("omo_manager.omo_codex_start.run", side_effect=fake_run) as run_mock, patch("omo_manager.omo_codex_start.exact_tail", side_effect=lambda *_: next(tails)), patch("omo_manager.omo_codex_start.extract_new_status_session_id", return_value=self.UUID), patch("omo_manager.omo_codex_start.verify_same_process"):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1))
        if_shell = next(call.args[0] for call in run_mock.call_args_list if call.args[0][:3] == ["tmux", "if-shell", "-F"])
        self.assertIn("#{pane_pid},42", if_shell[5])
        self.assertIn("#{pane_current_command},bun", if_shell[5])
        self.assertIn("#{pane_current_path},/tmp", if_shell[5])

    def test_status_submission_retries_dropped_first_enter_on_exact_retained_source(self) -> None:
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42, 7001)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        guarded_calls: list[list[str]] = []

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                guarded_calls.append(argv)
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        retained = ["› /status", "  gpt-5.6-terra high · /tmp · Context 0% used"]
        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch(
                "omo_manager.omo_codex_start.exact_tail",
                side_effect=((True, ["before"]), (True, self.READY), (True, retained), (True, ["after"])),
            ),
            patch("omo_manager.omo_codex_start.extract_new_status_session_id", side_effect=("", self.UUID)),
            patch("omo_manager.omo_codex_start.verify_same_process"),
            patch("omo_manager.omo_codex_start.exact_retained_status_action") as retry,
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.3, 0.6)),
            patch("omo_manager.omo_codex_start.time.sleep"),
        ):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1))

        self.assertEqual(1, len(guarded_calls))
        self.assertIn("paste-buffer", guarded_calls[0][6])
        retry.assert_called_once_with(pane, ANY, ANY, 1, submit=True)

    def test_exact_retained_status_retry_probes_source_before_enter(self) -> None:
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42, 7001)
        nonce = "123-456"
        probe = "__OMO_" + __import__("hashlib").sha256(f"{nonce}:1".encode()).hexdigest()[:16] + "__"
        footer = "  gpt-5.6-terra high · /tmp · Context 0% used"
        commands: list[list[str]] = []

        def fake_run(argv):
            commands.append(argv)
            token = accepted_tmux_marker(argv[6])
            return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")

        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch("omo_manager.omo_codex_start.verify_same_process"),
            patch("omo_manager.omo_codex_start.tmux_input_lock", side_effect=lambda _target: nullcontext()),
            patch("omo_manager.omo_codex_start.prompt_state", return_value=("running", [f"› /status{probe}", footer], f"/status{probe}")),
            patch("omo_manager.omo_codex_start.exact_status_probe_rendering", return_value=True),
        ):
            exact_retained_status_action(pane, "condition", nonce, 1, submit=True)

        self.assertEqual(2, len(commands))
        self.assertIn("send-keys -t %1 End", commands[0][6])
        self.assertIn(f"send-keys -l -t %1 -- {probe}", commands[0][6])
        self.assertIn(f"send-keys -N {len(probe)} -t %1 BSpace", commands[1][6])
        self.assertIn("send-keys -t %1 Enter", commands[1][6])

    def test_exact_retained_status_retry_rejects_whitespace_mutation(self) -> None:
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42, 7001)
        nonce = "123-456"
        probe = "__OMO_" + __import__("hashlib").sha256(f"{nonce}:1".encode()).hexdigest()[:16] + "__"
        footer = "  gpt-5.6-terra high · /tmp · Context 0% used"
        for mutated in (f"›  /status{probe}", f"› /status {probe}"):
            commands: list[list[str]] = []

            def fake_run(argv):
                commands.append(argv)
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")

            with (
                self.subTest(mutated=mutated),
                patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
                patch("omo_manager.omo_codex_start.verify_same_process"),
                patch("omo_manager.omo_codex_start.tmux_input_lock", side_effect=lambda _target: nullcontext()),
                patch(
                    "omo_manager.omo_codex_start.prompt_state",
                    side_effect=(("running", [mutated, footer], mutated.removeprefix("› ").strip()), ("running", ["› /status", footer], "/status")),
                ),
                patch("omo_manager.omo_codex_start.exact_status_probe_rendering", return_value=False),
                patch("omo_manager.omo_codex_start.exact_status_probe_suffix_at_cursor", return_value=True),
                patch("omo_manager.omo_codex_start.raw_status_composer_cursor", return_value=("› /status", len("› /status"))),
                self.assertRaisesRegex(PromptHandoffError, "unnormalized source binding"),
            ):
                exact_retained_status_action(pane, "condition", nonce, 1, submit=True)

            self.assertEqual(2, len(commands))
            self.assertIn(f"send-keys -N {len(probe)} -t %1 BSpace", commands[1][6])
            self.assertNotIn(" Enter", commands[1][6])

    def test_exact_retained_status_mismatch_never_cleans_past_trailing_space(self) -> None:
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42, 7001)
        nonce = "123-456"
        probe = "__OMO_" + __import__("hashlib").sha256(f"{nonce}:1".encode()).hexdigest()[:16] + "__"
        commands: list[list[str]] = []

        def fake_run(argv):
            commands.append(argv)
            token = accepted_tmux_marker(argv[6])
            return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")

        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch("omo_manager.omo_codex_start.verify_same_process"),
            patch("omo_manager.omo_codex_start.tmux_input_lock", side_effect=lambda _target: nullcontext()),
            patch("omo_manager.omo_codex_start.prompt_state", return_value=("running", [], f"/status{probe}")),
            patch("omo_manager.omo_codex_start.exact_status_probe_rendering", return_value=False),
            patch("omo_manager.omo_codex_start.exact_status_probe_suffix_at_cursor", return_value=False),
            self.assertRaisesRegex(PromptHandoffError, "changed while its source probe"),
        ):
            exact_retained_status_action(pane, "condition", nonce, 1, submit=True)

        self.assertEqual(1, len(commands))
        self.assertNotIn("BSpace", commands[0][6])
        self.assertNotIn(" Enter", commands[0][6])

    def test_exact_status_probe_rendering_preserves_trailing_space_and_cursor(self) -> None:
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42, 7001)
        probe = "__OMO_0123456789abcdef__"
        expected = f"› /status{probe}"
        exact_rows = [" " * 80, expected.ljust(80), " " * 80]
        changed_rows = [" " * 80, f"{expected} ".ljust(80), " " * 80]
        results = (
            __import__("subprocess").CompletedProcess([], 0, "\n".join(exact_rows) + "\n", ""),
            __import__("subprocess").CompletedProcess([], 0, f"{len(expected)}\t1\n", ""),
            __import__("subprocess").CompletedProcess([], 0, "\n".join(changed_rows) + "\n", ""),
            __import__("subprocess").CompletedProcess([], 0, f"{len(expected) + 1}\t1\n", ""),
        )
        with patch("omo_manager.omo_codex_start.run", side_effect=results), patch("omo_manager.omo_codex_start.verify_same_process"):
            self.assertTrue(exact_status_probe_rendering(pane, probe))
            self.assertFalse(exact_status_probe_rendering(pane, probe))

        suffix_results = (
            __import__("subprocess").CompletedProcess([], 0, "\n".join(exact_rows) + "\n", ""),
            __import__("subprocess").CompletedProcess([], 0, f"{len(expected)}\t1\n", ""),
            __import__("subprocess").CompletedProcess([], 0, "\n".join(changed_rows) + "\n", ""),
            __import__("subprocess").CompletedProcess([], 0, f"{len(expected) + 1}\t1\n", ""),
        )
        with patch("omo_manager.omo_codex_start.run", side_effect=suffix_results), patch("omo_manager.omo_codex_start.verify_same_process"):
            self.assertTrue(exact_status_probe_suffix_at_cursor(pane, probe))
            self.assertFalse(exact_status_probe_suffix_at_cursor(pane, probe))

    def test_exact_status_accepts_visible_bound_process_card(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        status = f"╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {self.UUID} │\n╰────╯"
        with patch("omo_manager.omo_codex_start.run", side_effect=fake_run), patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, [status]), (True, self.READY), (True, [status]))), patch("omo_manager.omo_codex_start.verify_same_process"):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1))

    def test_exact_status_ignores_stale_visible_uuid_until_new_response(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        new_session = "119f670b-6a2f-7463-b9be-9aa6ff0cec43"

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        old_status = f"/status\n╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {self.UUID} │\n╰────╯"
        new_card = f"╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {new_session} │\n╰────╯"
        new_status = f"{old_status}\n/status\n{new_card}"
        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, [old_status]), (True, self.READY), (True, [old_status]), (True, [new_status]))),
            patch("omo_manager.omo_codex_start.verify_same_process"),
            patch("omo_manager.omo_codex_start.time.sleep"),
        ):
            self.assertEqual(new_session, query_exact_status_session_id(pane, 80, 1, self.UUID))

    def test_exact_status_rejects_unframed_post_query_uuid(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        new_session = "119f670b-6a2f-7463-b9be-9aa6ff0cec43"
        before = f"/status\n╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {self.UUID} │\n╰────╯"
        after = f"{before}\n/status\n│ Session: {new_session} │"
        evidence: dict[str, str] = {}

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        with patch("omo_manager.omo_codex_start.run", side_effect=fake_run), patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, [before]), (True, self.READY), (True, [after]), (True, self.READY))), patch("omo_manager.omo_codex_start.verify_same_process"), patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.5, 1.0)), patch("omo_manager.omo_codex_start.time.sleep"):
            self.assertEqual("", query_exact_status_session_id(pane, 80, 1, self.UUID, evidence, require_complete_status_card=True))
        self.assertEqual("unframed", evidence["response-session-state"])
        self.assertEqual("", evidence["response-session-id"])

    def test_exact_status_returns_incumbent_from_new_response_for_hard_failure(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        status = f"╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {self.UUID} │\n╰────╯"

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, ["fresh pane"]), (True, self.READY), (True, [f"/status\n{status}"]))),
            patch("omo_manager.omo_codex_start.verify_same_process"),
        ):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1, self.UUID))

    def test_exact_status_does_not_accept_different_uuid_from_prior_history(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        prior = "119f670b-6a2f-7463-b9be-9aa6ff0cec43"
        before = f"/status\n│ Session: {prior} │"
        after = f"{before}\n/status\n│ Account: team@example.test │"

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, [before]), (True, self.READY), (True, [after]), (True, self.READY))),
            patch("omo_manager.omo_codex_start.verify_same_process"),
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.5, 1.0)),
            patch("omo_manager.omo_codex_start.time.sleep"),
        ):
            self.assertEqual("", query_exact_status_session_id(pane, 80, 1, self.UUID))

    def test_exact_status_recaptures_and_clears_delayed_retained_query_at_deadline(self) -> None:
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42, 7001)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = accepted_tmux_marker(argv[6])
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        retained = ["› /status", "  gpt-5.6-terra high · /tmp · Context 0% used"]
        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch(
                "omo_manager.omo_codex_start.exact_tail",
                side_effect=((True, ["before"]), (True, self.READY), (True, self.READY), (True, retained)),
            ),
            patch("omo_manager.omo_codex_start.verify_same_process"),
            patch("omo_manager.omo_codex_start.clear_retained_status_query") as clear,
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.5, 1.0)),
            patch("omo_manager.omo_codex_start.time.sleep"),
            self.assertRaisesRegex(StartError, "exact retained input was cleared"),
        ):
            query_exact_status_session_id(pane, 80, 1)

        clear.assert_called_once()

    def test_exact_status_does_not_submit_from_plan_or_non_codex_ui(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        unsafe_states = (
            ["Create a plan? shift + tab use Plan mode esc dismiss", "› choose an option", "  gpt-5.6-terra high"],
            ["user@host:~$"],
            ["■ Error: 429 Too Many Requests", "› Use /skills to list available skills", "  gpt-5.6-terra high"],
        )
        for unsafe in unsafe_states:
            calls: list[list[str]] = []

            def fake_run(argv):
                calls.append(argv)
                return __import__("subprocess").CompletedProcess(argv, 0, "", "")

            with self.subTest(unsafe=unsafe), patch("omo_manager.omo_codex_start.run", side_effect=fake_run), patch(
                "omo_manager.omo_codex_start.exact_tail", side_effect=((True, ["before"]), (True, unsafe))
            ), patch("omo_manager.omo_codex_start.verify_same_process"), self.assertRaises(StartError):
                query_exact_status_session_id(pane, 80, 1)

            self.assertFalse(any(call[:3] == ["tmux", "if-shell", "-F"] for call in calls))

    def test_exact_status_rejects_plain_stale_session_line(self):
        from omo_manager.omo_codex_start import visible_status_card_session_id

        self.assertEqual("", visible_status_card_session_id(f"old transcript says Session: {self.UUID}"))


if __name__ == "__main__":
    unittest.main()
