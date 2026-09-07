from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_status import Report
from omo_manager.omo_manager_rotate import ProcessInfo
from omo_manager.omo_pb_watcher_cursor_replace import (
    SCHEMA,
    TARGET,
    AUTHORITY_TEXT,
    Args,
    AuthorityProof,
    Binding,
    CursorProof,
    FileProof,
    LifecycleProof,
    PaneProof,
    ProtectedProof,
    ReplaceError,
    RuntimeProof,
    atomic_respawn,
    authority_proof,
    binding_sha256,
    file_proof,
    finish_audit,
    live_marker,
    parse_args,
    read_reconcilable_audit,
    replacement_command,
    reserve_audit,
    require_isolated_pane_process_tree,
    replace_watcher,
    verify_unchanged,
    wait_ready_empty,
)


def file_value(name: str, digest: str = "1" * 64) -> FileProof:
    return FileProof(name, True, 1, 2, 3, 4, 0o600, digest)


def pane(pid: int = 100, *, command: str = "agent", attached: bool = False) -> PaneProof:
    return PaneProof(TARGET, "$1", "@2", "%3", pid, pid * 10, command, "/ssd1/sichangheagent/personal_browser_setup", attached)


def lifecycle(task_digest: str = "a" * 64) -> LifecycleProof:
    return LifecycleProof(file_value("task", task_digest), file_value("todo", "b" * 64), "long_running", "cursor", TARGET, "pb:13", "c" * 64, 2, "d" * 64, 1)


def protected(cdp: str = "e" * 64) -> ProtectedProof:
    return ProtectedProof(file_value("env"), (file_value("db"), FileProof("wal", False), FileProof("shm", False)), (pane(10), pane(11), pane(12), pane(13)), cdp, "8" * 64, False)


def runtime() -> RuntimeProof:
    return RuntimeProof("1" * 64, "2" * 64, "3" * 64, "fresh-no-prompt", "cursor-grok-4.6", "low", "5" * 64)


def authority() -> AuthorityProof:
    return AuthorityProof(file_value("authority"), (3, 7), "6" * 64)


def binding(*, task_digest: str = "a" * 64, cdp: str = "e" * 64) -> Binding:
    return Binding(SCHEMA, pane(), CursorProof(101, 1010, "/node", "f" * 64), "0" * 64, lifecycle(task_digest), protected(cdp), runtime(), authority())


def watcher_values() -> dict[str, str]:
    return {"PB_WATCHER_AGENT_MODEL": "cursor-grok-4.6", "PB_WATCHER_AGENT_REASONING_EFFORT": "low"}


class PbWatcherCursorReplaceTests(unittest.TestCase):
    def test_parse_args_is_pinned_and_requires_execution_evidence(self) -> None:
        base = [
            "--root",
            "/ssd1/sichangheagent/work_logs",
            "--task-file",
            "202607/pbw_interpreter_live.md",
            "--target",
            TARGET,
        ]
        described = parse_args([*base, "--describe"])
        self.assertTrue(described.describe)
        with self.assertRaises(SystemExit):
            parse_args(base)
        with self.assertRaises(SystemExit):
            parse_args([*base[:-1], "hpb:0", "--describe"])
        with self.assertRaises(SystemExit):
            parse_args([*base, "--expected-binding-sha256", "1" * 64, "--audit-output", "/tmp/outside.audit"])
        state = Path.home() / ".local/state/omo-manager"
        executed = parse_args(
            [
                *base,
                "--expected-binding-sha256",
                "1" * 64,
                "--audit-output",
                str(state / "rotations/pb.audit"),
            ]
        )
        self.assertFalse(executed.describe)

    def test_live_marker_requires_one_complete_gmail_block(self) -> None:
        text = "header\n(pending)\nKeep the Gmail mail blocker.\n(pending items recorded line=3: n=1 sha256=abc)\n"
        self.assertEqual(hashlib.sha256(live_marker(text)).hexdigest(), hashlib.sha256(text[text.index("(pending)") :].encode()).hexdigest())
        with self.assertRaisesRegex(ReplaceError, "exactly one"):
            live_marker(text + "(pending)\nGmail mail\n(pending items recorded line=6: n=1 sha256=def)\n")
        with self.assertRaisesRegex(ReplaceError, "not the expected Gmail"):
            live_marker("(pending)\nother work\n(pending items recorded line=2: n=1 sha256=abc)\n")

    def test_authority_requires_the_exact_owner_private_human_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail"
            mail.mkdir(mode=0o700)
            source = mail / "85c5dff58359-1468.txt"
            source.write_text("Subject: test\n\n" + AUTHORITY_TEXT + "\n", encoding="utf-8")
            source.chmod(0o600)
            args = Args(root, "202607/pbw_interpreter_live.md", TARGET, "", None, root, 1, 0.1, True)
            self.assertEqual((3, 7), authority_proof(args).lines)
            source.write_text("Subject: test\n\nDo not replace it.\n", encoding="utf-8")
            source.chmod(0o600)
            with self.assertRaisesRegex(ReplaceError, "exactly authorize"):
                authority_proof(args)

    def test_complete_pane_tree_rejects_unbound_sibling_work(self) -> None:
        current_pane = pane()
        cursor = CursorProof(102, 1020, "/node", "f" * 64)
        clean = {
            100: ProcessInfo(100, 1, "S", ("zsh",)),
            101: ProcessInfo(101, 100, "S", ("fish",)),
            102: ProcessInfo(102, 101, "S", ("agent",)),
        }
        with patch("omo_manager.omo_pb_watcher_cursor_replace.read_processes", return_value=clean):
            require_isolated_pane_process_tree(current_pane, cursor)
        dirty = dict(clean)
        dirty[103] = ProcessInfo(103, 101, "S", ("python", "job.py"))
        with (
            patch("omo_manager.omo_pb_watcher_cursor_replace.read_processes", return_value=dirty),
            self.assertRaisesRegex(ReplaceError, "sibling or child"),
        ):
            require_isolated_pane_process_tree(current_pane, cursor)

    def test_binding_digest_and_protected_drift_fail_closed(self) -> None:
        original = binding()
        self.assertEqual(binding_sha256(original), binding_sha256(original))
        with self.assertRaisesRegex(ReplaceError, "database, browser"):
            verify_unchanged(original, binding(cdp="9" * 64), after_replacement=True)
        with self.assertRaisesRegex(ReplaceError, "pane/process/composer"):
            verify_unchanged(original, Binding(SCHEMA, pane(200), original.cursor, original.retained_composer_sha256, original.lifecycle, original.protected, original.runtime, original.authority))

    def test_atomic_respawn_uses_all_tmux_identities_and_requires_new_pid(self) -> None:
        old = pane()
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("omo_manager.omo_pb_watcher_cursor_replace.run_tmux", return_value=completed) as run_tmux,
            patch("omo_manager.omo_pb_watcher_cursor_replace.pane_proof", return_value=pane(200)),
        ):
            atomic_respawn(old, "exec agent", {"tmux_path": "/tmux"})
        arguments = run_tmux.call_args.args[0]
        condition = arguments[4]
        for exact in (old.session_id, old.window_id, old.pane_id, old.target, str(old.pane_pid), old.command):
            self.assertIn(exact, condition)
        self.assertIn("respawn-pane -k", arguments[5])
        with (
            patch("omo_manager.omo_pb_watcher_cursor_replace.run_tmux", return_value=completed),
            patch("omo_manager.omo_pb_watcher_cursor_replace.pane_proof", return_value=old),
            self.assertRaisesRegex(ReplaceError, "completion-unknown"),
        ):
            atomic_respawn(old, "exec agent", {"tmux_path": "/tmux"})

    def test_wait_ready_requires_exact_process_environment_and_empty_composer(self) -> None:
        old = pane()
        new = pane(200)
        cursor = CursorProof(200, 2000, "/node", "f" * 64)
        environment = {"HOME": "/home/test"}
        with (
            patch("omo_manager.omo_pb_watcher_cursor_replace.pane_proof", return_value=new),
            patch("omo_manager.omo_pb_watcher_cursor_replace.cursor_candidates", return_value=[cursor]),
            patch("omo_manager.omo_pb_watcher_cursor_replace.cursor_runtime_identity", return_value={}),
            patch("omo_manager.omo_pb_watcher_cursor_replace.pinned_tmux_identity", return_value={}),
            patch("omo_manager.omo_pb_watcher_cursor_replace.exact_process_environment", return_value=environment),
            patch("omo_manager.omo_pb_watcher_cursor_replace.inspect", return_value=Report("ready", [])),
            patch("omo_manager.omo_pb_watcher_cursor_replace.capture_lines", return_value=["footer"]),
            patch("omo_manager.omo_pb_watcher_cursor_replace.current_input_text", return_value="Add a follow-up"),
            patch("omo_manager.omo_pb_watcher_cursor_replace.is_cursor_retained_submitted_composer", return_value=False),
            patch("omo_manager.omo_pb_watcher_cursor_replace.has_cursor_followups_overlay", return_value=False),
            patch("omo_manager.omo_pb_watcher_cursor_replace.has_cursor_agent_running_indicator", return_value=False),
        ):
            self.assertEqual((new, cursor), wait_ready_empty(old, b"", "cursor-grok-4.6-low", environment, {}, {}, 0.1, 0.01))
        with (
            patch("omo_manager.omo_pb_watcher_cursor_replace.pane_proof", return_value=new),
            patch("omo_manager.omo_pb_watcher_cursor_replace.cursor_candidates", return_value=[cursor]),
            patch("omo_manager.omo_pb_watcher_cursor_replace.cursor_runtime_identity", return_value={}),
            patch("omo_manager.omo_pb_watcher_cursor_replace.pinned_tmux_identity", return_value={}),
            patch("omo_manager.omo_pb_watcher_cursor_replace.exact_process_environment", return_value={"WRONG": "1"}),
            self.assertRaisesRegex(ReplaceError, "sanitized environment"),
        ):
            wait_ready_empty(old, b"", "cursor-grok-4.6-low", environment, {}, {}, 0.1, 0.01)

    def test_wait_ready_rejects_an_extra_cursor_process(self) -> None:
        old = pane()
        new = pane(200)
        exact = CursorProof(200, 2000, "/node", "f" * 64)
        extra = CursorProof(201, 2010, "/node", "e" * 64)
        with (
            patch("omo_manager.omo_pb_watcher_cursor_replace.pane_proof", return_value=new),
            patch("omo_manager.omo_pb_watcher_cursor_replace.cursor_candidates", side_effect=[[exact, extra], [exact]]),
            patch("omo_manager.omo_pb_watcher_cursor_replace.cursor_runtime_identity", return_value={}),
            patch("omo_manager.omo_pb_watcher_cursor_replace.pinned_tmux_identity", return_value={}),
            self.assertRaisesRegex(ReplaceError, "exactly one total Cursor"),
        ):
            wait_ready_empty(old, b"", "cursor-grok-4.6-low", {}, {}, {}, 0.1, 0.01)

    def test_wait_ready_rejects_an_attached_replacement_session(self) -> None:
        with (
            patch("omo_manager.omo_pb_watcher_cursor_replace.pane_proof", return_value=pane(200, attached=True)),
            self.assertRaisesRegex(ReplaceError, "became attached"),
        ):
            wait_ready_empty(pane(), b"", "cursor-grok-4.6-low", {}, {}, {}, 0.1, 0.01)

    def test_replacement_has_no_startup_prompt_and_uses_canonical_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            args = Args(
                Path("/ssd1/sichangheagent/work_logs"),
                "202607/pbw_interpreter_live.md",
                TARGET,
                "1" * 64,
                directory / "audit",
                directory,
                1,
                0.1,
                False,
            )
            values = {"PB_WATCHER_AGENT_MODEL": "cursor-grok-4.6", "PB_WATCHER_AGENT_REASONING_EFFORT": "low"}
            runtime_value = {
                "launcher_resolved": "/cursor/agent",
                "node_path": "/cursor/node",
                "index_path": "/cursor/index.js",
            }
            with patch(
                "omo_manager.omo_pb_watcher_cursor_replace.cursor_process_environment",
                return_value={"OMO_AGENT_TMUX_TARGET": "pb-newswatcher-agent:0"},
            ), patch(
                "omo_manager.omo_pb_watcher_cursor_replace.pinned_shell_identity",
                return_value={"env_path": "/usr/bin/env", "bash_path": "/bin/bash"},
            ):
                rendered, environment = replacement_command(args, pane(), values, runtime_value)
            self.assertEqual("pb-newswatcher-agent:0", environment["OMO_AGENT_TMUX_TARGET"])
            self.assertIn("OMO_AGENT_TMUX_TARGET=pb-newswatcher-agent:0", rendered)
            self.assertNotIn("--resume", rendered)
            self.assertNotIn("continue", rendered)
            self.assertNotIn("cd /ssd1", rendered)
            shell_environment = {
                "HOME": "/home/sichangheagent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "LOGNAME": "sichangheagent",
                "PATH": "/usr/bin:/bin",
                "TERM": "xterm-256color",
                "USER": "sichangheagent",
                "OMO_AGENT_TMUX_TARGET": "pb-newswatcher-agent:0",
                "OMO_WORK_LOGS_ROOT": "/ssd1/sichangheagent/work_logs",
            }
            observed = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-c", "exec /usr/bin/env"],
                cwd="/ssd1/sichangheagent/personal_browser_setup",
                env=shell_environment,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertNotIn("OLDPWD=", observed)
            self.assertIn("PWD=/ssd1/sichangheagent/personal_browser_setup\n", observed)

    def test_private_audit_commits_only_the_bound_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            path = directory / "replacement.audit"
            original = binding()
            prepared = reserve_audit(path, original, binding_sha256(original))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            finish_audit(path, prepared, "committed", new_pane=pane(200), new_cursor=CursorProof(200, 2000, "/node", "9" * 64))
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("committed", record["state"])
            self.assertEqual(1, record["terminal_authoritative_owner_count"])
            self.assertEqual("empty", record["terminal_composer"])
            self.assertEqual(200, record["new_cursor_pid"])

    def test_full_replacement_flow_commits_after_all_postchecks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / "state"
            state.mkdir(mode=0o700)
            audit = state / "replacement.audit"
            original = binding()
            new_pane = pane(200)
            new_cursor = CursorProof(200, 2000, "/node", "9" * 64)
            args = Args(directory, "202607/pbw_interpreter_live.md", TARGET, binding_sha256(original), audit, state, 1, 0.1, False)
            def context(*_args: object, **_kwargs: object):
                return nullcontext()

            def prove(path: Path, **kwargs: object) -> FileProof:
                return file_proof(path, **kwargs) if Path(path) == audit else original.protected.environment

            with (
                patch.dict(os.environ, {"TMUX_PANE": ""}, clear=False),
                patch("omo_manager.omo_pb_watcher_cursor_replace.tmux_input_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.root_membership_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_target_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_file_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.require_no_legacy_sender"),
                patch("omo_manager.omo_pb_watcher_cursor_replace.capture_binding", return_value=(original, watcher_values(), {}, {})),
                patch("omo_manager.omo_pb_watcher_cursor_replace.replacement_command", return_value=("exec agent", {})),
                patch("omo_manager.omo_pb_watcher_cursor_replace.atomic_respawn") as respawn,
                patch("omo_manager.omo_pb_watcher_cursor_replace.wait_ready_empty", return_value=(new_pane, new_cursor)),
                patch("omo_manager.omo_pb_watcher_cursor_replace.process_start_ticks", side_effect=ReplaceError("gone")),
                patch("omo_manager.omo_pb_watcher_cursor_replace.lifecycle_proof", return_value=original.lifecycle),
                patch("omo_manager.omo_pb_watcher_cursor_replace.file_proof", side_effect=prove),
                patch("omo_manager.omo_pb_watcher_cursor_replace.parse_env", return_value={}),
                patch("omo_manager.omo_pb_watcher_cursor_replace.protected_proof", return_value=original.protected),
                patch("omo_manager.omo_pb_watcher_cursor_replace.authoritative_active_target_task_paths", return_value=((directory / "202607/pbw_interpreter_live.md").resolve(),)),
                patch("omo_manager.omo_pb_watcher_cursor_replace.require_isolated_pane_process_tree") as isolate,
            ):
                result = replace_watcher(args)
            self.assertIn("replaced", result)
            respawn.assert_called_once()
            isolate.assert_called_once_with(new_pane, new_cursor)
            self.assertEqual("committed", json.loads(audit.read_text(encoding="utf-8"))["state"])

    def test_completion_unknown_audit_reconciles_without_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / "state"
            state.mkdir(mode=0o700)
            audit = state / "replacement.audit"
            original = binding()
            prepared = reserve_audit(audit, original, binding_sha256(original))
            finish_audit(audit, prepared, "completion-unknown", detail="simulated crash boundary")
            audit_sha = hashlib.sha256(audit.read_bytes()).hexdigest()
            current = Binding(SCHEMA, pane(200), CursorProof(200, 2000, "/node", "9" * 64), "0" * 64, original.lifecycle, original.protected, original.runtime, original.authority)
            args = Args(directory, "202607/pbw_interpreter_live.md", TARGET, "", audit, state, 1, 0.1, False, True, audit_sha)
            def context(*_args: object, **_kwargs: object):
                return nullcontext()
            with (
                patch("omo_manager.omo_pb_watcher_cursor_replace.tmux_input_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.root_membership_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_target_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_file_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.require_no_legacy_sender"),
                patch("omo_manager.omo_pb_watcher_cursor_replace.capture_binding", return_value=(current, watcher_values(), {}, {})),
                patch("omo_manager.omo_pb_watcher_cursor_replace.cursor_process_environment", return_value={}),
                patch("omo_manager.omo_pb_watcher_cursor_replace.wait_ready_empty", return_value=(current.target_pane, current.cursor)),
                patch("omo_manager.omo_pb_watcher_cursor_replace.process_start_ticks", side_effect=ReplaceError("gone")),
                patch("omo_manager.omo_pb_watcher_cursor_replace.authoritative_active_target_task_paths", return_value=((directory / "202607/pbw_interpreter_live.md").resolve(),)),
                patch("omo_manager.omo_pb_watcher_cursor_replace.atomic_respawn") as respawn,
                patch("omo_manager.omo_pb_watcher_cursor_replace.require_isolated_pane_process_tree"),
            ):
                result = replace_watcher(args)
            self.assertIn("reconciled", result)
            respawn.assert_not_called()
            self.assertEqual("committed", json.loads(audit.read_text(encoding="utf-8"))["state"])

    def test_pre_respawn_crash_reconciles_as_aborted_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / "state"
            state.mkdir(mode=0o700)
            audit = state / "replacement.audit"
            original = binding()
            _ = reserve_audit(audit, original, binding_sha256(original))
            audit_sha = hashlib.sha256(audit.read_bytes()).hexdigest()
            args = Args(directory, "202607/pbw_interpreter_live.md", TARGET, "", audit, state, 1, 0.1, False, True, audit_sha)

            def context(*_args: object, **_kwargs: object):
                return nullcontext()

            with (
                patch("omo_manager.omo_pb_watcher_cursor_replace.tmux_input_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.root_membership_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_target_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_file_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.require_no_legacy_sender"),
                patch("omo_manager.omo_pb_watcher_cursor_replace.capture_binding", return_value=(original, watcher_values(), {}, {})),
                patch("omo_manager.omo_pb_watcher_cursor_replace.atomic_respawn") as respawn,
                patch("omo_manager.omo_pb_watcher_cursor_replace.wait_ready_empty") as wait,
            ):
                result = replace_watcher(args)
            self.assertIn("aborted-no-respawn", result)
            respawn.assert_not_called()
            wait.assert_not_called()
            self.assertEqual("aborted-no-respawn", json.loads(audit.read_text(encoding="utf-8"))["state"])

    def test_post_tmux_error_keeps_a_reconcilable_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / "state"
            state.mkdir(mode=0o700)
            audit = state / "replacement.audit"
            original = binding()
            args = Args(directory, "202607/pbw_interpreter_live.md", TARGET, binding_sha256(original), audit, state, 1, 0.1, False)

            def context(*_args: object, **_kwargs: object):
                return nullcontext()

            with (
                patch.dict(os.environ, {"TMUX_PANE": ""}, clear=False),
                patch("omo_manager.omo_pb_watcher_cursor_replace.tmux_input_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.root_membership_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_target_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.task_file_lock", side_effect=context),
                patch("omo_manager.omo_pb_watcher_cursor_replace.require_no_legacy_sender"),
                patch("omo_manager.omo_pb_watcher_cursor_replace.capture_binding", return_value=(original, watcher_values(), {}, {})),
                patch("omo_manager.omo_pb_watcher_cursor_replace.replacement_command", return_value=("exec agent", {})),
                patch("omo_manager.omo_pb_watcher_cursor_replace.atomic_respawn", side_effect=ReplaceError("post-command proof failed")),
                self.assertRaisesRegex(ReplaceError, "post-command proof failed"),
            ):
                replace_watcher(args)
            self.assertEqual("completion-unknown", json.loads(audit.read_text(encoding="utf-8"))["state"])
            audit_sha = hashlib.sha256(audit.read_bytes()).hexdigest()
            _data, record = read_reconcilable_audit(audit, audit_sha)
            self.assertEqual("completion-unknown", record["state"])


if __name__ == "__main__":
    unittest.main()
