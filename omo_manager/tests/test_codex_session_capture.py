import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from unittest.mock import ANY, patch
from omo_manager.omo_codex_start import Args, Pane, StartError, launch_command, query_exact_status_session_id, record_session_id
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_codex_session_migrate import CODEX_PANE_COMMANDS, authorized_human_targets, candidates, run as run_migration, submit_existing_input


class CodexSessionCaptureTests(unittest.TestCase):
    UUID = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"

    def test_migration_accepts_live_bunx_launcher_command(self):
        self.assertIn("bunx", CODEX_PANE_COMMANDS)

    def test_existing_input_enter_is_exact_process_guarded(self):
        pane = Pane("w:1.0", "%1", "@1", "bunx", Path("/tmp"), 42)

        def run(argv, **_kwargs):
            accepted = argv[6].split("display-message -p ", 1)[1]
            return __import__("subprocess").CompletedProcess(argv, 0, accepted + "\n", "")

        with patch("omo_manager.omo_codex_session_migrate.subprocess.run", side_effect=run) as mocked:
            submit_existing_input(pane)
        argv = mocked.call_args.args[0]
        self.assertIn("#{pane_id},%1", argv[5])
        self.assertIn("#{window_id},@1", argv[5])
        self.assertIn("#{session_name}:#{window_index}.#{pane_index},w:1.0", argv[5])
        self.assertIn("#{pane_pid},42", argv[5])
        self.assertIn("#{pane_current_command},bunx", argv[5])
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

    def test_rejects_digest_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text("---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: codex\nmanagerat: w:2\nis_manager: false\npending_task_items: []\n---\n")
            path.write_text(path.read_text() + "drift")
            with self.assertRaises(StartError):
                record_session_id(path, self.UUID, "0" * 64)

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
                token = next(part for part in argv[6].split(" ; ") if part.startswith("display-message -p ")).split()[-1]
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        tails = iter(((True, ["before"]), (True, ["after"])))
        with patch("omo_manager.omo_codex_start.run", side_effect=fake_run) as run_mock, patch("omo_manager.omo_codex_start.exact_tail", side_effect=lambda *_: next(tails)), patch("omo_manager.omo_codex_start.extract_new_status_session_id", return_value=self.UUID), patch("omo_manager.omo_codex_start.verify_same_process"):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1))
        if_shell = next(call.args[0] for call in run_mock.call_args_list if call.args[0][:3] == ["tmux", "if-shell", "-F"])
        self.assertIn("#{pane_pid},42", if_shell[5])
        self.assertIn("#{pane_current_command},bun", if_shell[5])

    def test_exact_status_accepts_visible_bound_process_card(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = next(part for part in argv[6].split(" ; ") if part.startswith("display-message -p ")).split()[-1]
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        status = f"╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {self.UUID} │\n╰────╯"
        with patch("omo_manager.omo_codex_start.run", side_effect=fake_run), patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, [status]), (True, [status]))), patch("omo_manager.omo_codex_start.verify_same_process"):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1))

    def test_exact_status_ignores_stale_visible_uuid_until_new_response(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        new_session = "119f670b-6a2f-7463-b9be-9aa6ff0cec43"

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = next(part for part in argv[6].split(" ; ") if part.startswith("display-message -p ")).split()[-1]
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        status = f"╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {self.UUID} │\n╰────╯"
        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, [status]), (True, [status]), (True, [status]))),
            patch("omo_manager.omo_codex_start.extract_new_status_session_id", side_effect=("", new_session)),
            patch("omo_manager.omo_codex_start.verify_same_process"),
            patch("omo_manager.omo_codex_start.time.sleep"),
        ):
            self.assertEqual(new_session, query_exact_status_session_id(pane, 80, 1, self.UUID))

    def test_exact_status_returns_newly_visible_old_uuid_for_freshness_rejection(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        status = f"╭────╮\n│ >_ OpenAI Codex (v0.150.1) │\n│ Session: {self.UUID} │\n╰────╯"

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = next(part for part in argv[6].split(" ; ") if part.startswith("display-message -p ")).split()[-1]
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        with (
            patch("omo_manager.omo_codex_start.run", side_effect=fake_run),
            patch("omo_manager.omo_codex_start.exact_tail", side_effect=((True, ["fresh pane"]), (True, [status]))),
            patch("omo_manager.omo_codex_start.extract_new_status_session_id", return_value=""),
            patch("omo_manager.omo_codex_start.verify_same_process"),
        ):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1, self.UUID))

    def test_exact_status_rejects_plain_stale_session_line(self):
        from omo_manager.omo_codex_start import visible_status_card_session_id

        self.assertEqual("", visible_status_card_session_id(f"old transcript says Session: {self.UUID}"))


if __name__ == "__main__":
    unittest.main()
