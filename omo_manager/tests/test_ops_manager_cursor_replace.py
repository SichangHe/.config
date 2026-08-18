from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_start import Pane
from omo_manager.omo_codex_status import Report
from omo_manager.omo_manager_rotate import PaneIdentity, ProcessInfo
from omo_manager.omo_ops_manager_cursor_replace import (
    AUTHORITY_RELATIVE,
    AUTHORITY_TEXT,
    CONTINUATION,
    REQUIRED_TARGET,
    TASK_NAME,
    Args,
    ReplaceError,
    bind,
    cursor_command,
    parse_args,
    read_authority,
    replace_manager,
    require_known_dirty_owners,
    require_unique_task,
)
from omo_manager.omo_task_metadata import parse_task_metadata


def task_text(*, tool: str = "codex", runat: str = "wl:3", managerat: str = "wl:18", pending: tuple[str, ...] = ("Replace wl:3 with Cursor",), body: str = "role\n") -> str:
    items = "\n".join(f"  - {item}" for item in pending)
    pending_block = "pending_task_items:\n" + items + "\n" if pending else "pending_task_items: []\n"
    return (
        "---\n"
        "version: v1.0.0\n"
        "status: long_running\n"
        f"runat: {runat}\n"
        f"tool: {tool}\n"
        f"managerat: {managerat}\n"
        "is_manager: true\n"
        f"{pending_block}"
        "---\n"
        f"{body}"
    )


def child_text(runat: str = "cfg:1") -> str:
    return task_text(runat=runat, managerat="wl:3", pending=())


def authority_bytes() -> str:
    return "".join(f"line {index}\n" for index in range(1, 16)) + f"{AUTHORITY_TEXT}\n"


def init_git(root: Path) -> None:
    _ = subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    _ = subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    _ = subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "--no-gpg-sign", "-m", "init"],
        check=True,
        capture_output=True,
    )


def write_ops_root(root: Path, *, pending_marker: bool = False) -> Path:
    root.chmod(0o700)
    mail = root / "manager_mail"
    mail.mkdir(mode=0o700)
    source = mail / Path(AUTHORITY_RELATIVE).name
    source.write_text(authority_bytes(), encoding="utf-8")
    source.chmod(0o600)
    (root / "MANAGER.md").write_text("manager instructions\n", encoding="utf-8")
    (root / "TODO.md").write_text("current\nops_manager.md wl:3\nchild.md cfg:1\n", encoding="utf-8")
    task = root / TASK_NAME
    task.write_text(task_text(body="role\n" + ("(pending)\nwait\n" if pending_marker else "")), encoding="utf-8")
    (root / "child.md").write_text(child_text(), encoding="utf-8")
    init_git(root)
    source.chmod(0o600)
    mail.chmod(0o700)
    root.chmod(0o700)
    return task


def helper_args(root: Path, state_dir: Path, *, dry_run: bool = False) -> Args:
    return Args(root, TASK_NAME, REQUIRED_TARGET, Path(AUTHORITY_RELATIVE), (16, 16), state_dir, 0.2, 0.01, dry_run)


def pane_for(root: Path, pid: int = 100) -> Pane:
    return Pane("wl:3.0", "%424242", "@9", "bunx", root, pid)


def identity_for(pane: Pane) -> PaneIdentity:
    return PaneIdentity(pane.target, pane.pane_id, pane.window_id, pane.pane_pid, pane.workdir)


def live_codex_processes(pane: Pane) -> dict[int, ProcessInfo]:
    return {
        pane.pane_pid: ProcessInfo(pane.pane_pid, 1, "S", ("zsh",)),
        pane.pane_pid + 1: ProcessInfo(pane.pane_pid + 1, pane.pane_pid, "S", ("/bin/bunx", "@openai/codex", "--model", "gpt-5.6-terra")),
    }


class OpsManagerCursorReplaceTests(unittest.TestCase):
    def test_parse_args_is_pinned_and_refuses_h_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def argv(**updates: str) -> list[str]:
                values = {
                    "--root": str(root),
                    "--task-file": TASK_NAME,
                    "--target": "wl:3",
                    "--authority-file": AUTHORITY_RELATIVE,
                    "--authority-lines": "16-16",
                }
                values.update(updates)
                parts: list[str] = []
                for key, value in values.items():
                    parts.extend((key, value))
                return parts

            args = parse_args(argv())
            self.assertEqual(TASK_NAME, args.task_file)
            self.assertEqual("wl:3", args.target)
            self.assertEqual("wl:3.0", parse_args(argv(**{"--target": "wl:3.0"})).target)
            with self.assertRaises(SystemExit):
                parse_args(argv(**{"--target": "hwl:3"}))
            with self.assertRaises(SystemExit):
                parse_args(argv(**{"--task-file": "other.md"}))
            with self.assertRaises(SystemExit):
                parse_args(argv(**{"--authority-lines": "16-17"}))
            with self.assertRaises(SystemExit):
                parse_args(argv(**{"--target": "wl:18"}))

    def test_authority_requires_exact_human_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            write_ops_root(root)
            args = helper_args(root, state)
            authority = read_authority(args)
            self.assertEqual(AUTHORITY_TEXT, authority.excerpt)
            (root / AUTHORITY_RELATIVE).write_text(authority_bytes().replace(AUTHORITY_TEXT, "Replace wl:3 with Codex"), encoding="utf-8")
            (root / AUTHORITY_RELATIVE).chmod(0o600)
            with self.assertRaisesRegex(ReplaceError, "exact approved human line"):
                read_authority(args)

    def test_unique_task_and_pending_delivery_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = write_ops_root(root)
            metadata, children = require_unique_task(root, task)
            self.assertEqual(("Replace wl:3 with Cursor",), metadata.pending_task_items)
            self.assertEqual(1, len(children))
            self.assertEqual("child.md", children[0].path.name)
            rival = root / "rival.md"
            rival.write_text(task_text(pending=()), encoding="utf-8")
            (root / "TODO.md").write_text("current\nops_manager.md wl:3\nrival.md wl:3\nchild.md cfg:1\n", encoding="utf-8")
            with self.assertRaisesRegex(ReplaceError, "ambiguous ownership"):
                require_unique_task(root, task)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = write_ops_root(root, pending_marker=True)
            with self.assertRaisesRegex(ReplaceError, "pending delivery"):
                require_unique_task(root, task)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = write_ops_root(root)
            task.write_text(task_text(managerat="wl:1"), encoding="utf-8")
            with self.assertRaisesRegex(ReplaceError, "managerat drifted"):
                require_unique_task(root, task)

    def test_dirty_unknown_state_fails_and_known_child_dirt_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = write_ops_root(root)
            notes = root / "NOTES.md"
            notes.write_text("notes\n", encoding="utf-8")
            _ = subprocess.run(["git", "-C", str(root), "add", "NOTES.md"], check=True, capture_output=True)
            _ = subprocess.run(
                ["git", "-C", str(root), "-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "--no-gpg-sign", "-m", "notes"],
                check=True,
                capture_output=True,
            )
            notes.write_text("dirty notes\n", encoding="utf-8")
            with self.assertRaisesRegex(ReplaceError, "dirty unknown state"):
                require_known_dirty_owners(root, task)
            notes.write_text("notes\n", encoding="utf-8")
            (root / "child.md").write_text(child_text() + "comment\n", encoding="utf-8")
            require_known_dirty_owners(root, task)

    def test_dry_run_and_success_preserve_queue_and_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            task = write_ops_root(root)
            child_before = (root / "child.md").read_text(encoding="utf-8")
            pending_before = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            assert pending_before is not None
            pane = pane_for(root)
            later = pane_for(root, pid=200)
            processes = live_codex_processes(pane)
            args = helper_args(root, state, dry_run=True)
            with (
                patch("omo_manager.omo_ops_manager_cursor_replace.resolve_exact_pane", return_value=identity_for(pane)),
                patch("omo_manager.omo_ops_manager_cursor_replace.resolve_pane", return_value=pane),
                patch("omo_manager.omo_ops_manager_cursor_replace.read_processes", return_value=processes),
                patch("omo_manager.omo_ops_manager_cursor_replace.inspect", return_value=Report("running", [])),
                patch("omo_manager.omo_ops_manager_cursor_replace.shutil.which", return_value="/usr/bin/agent"),
                patch.dict(os.environ, {"TMUX_PANE": "%other"}, clear=False),
                patch("omo_manager.omo_ops_manager_cursor_replace.respawn_codex") as respawn,
            ):
                self.assertEqual("dry-run", replace_manager(args))
                respawn.assert_not_called()
            after_dry = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            assert after_dry is not None
            self.assertEqual("codex", after_dry.tool)
            live_args = helper_args(root, state)
            resolve_calls = {"n": 0}

            def fake_resolve(_target: str) -> Pane:
                resolve_calls["n"] += 1
                return later if resolve_calls["n"] >= 3 else pane

            with (
                patch("omo_manager.omo_ops_manager_cursor_replace.resolve_exact_pane", return_value=identity_for(pane)),
                patch("omo_manager.omo_ops_manager_cursor_replace.resolve_pane", side_effect=fake_resolve),
                patch("omo_manager.omo_ops_manager_cursor_replace.read_processes", return_value=processes),
                patch("omo_manager.omo_ops_manager_cursor_replace.inspect", return_value=Report("running", [])),
                patch("omo_manager.omo_ops_manager_cursor_replace.shutil.which", return_value="/usr/bin/agent"),
                patch("omo_manager.omo_ops_manager_cursor_replace.pane_has_exact_cursor_process", return_value=True),
                patch("omo_manager.omo_ops_manager_cursor_replace.wait_for_cursor", return_value="running"),
                patch.dict(os.environ, {"TMUX_PANE": "%other"}, clear=False),
                patch("omo_manager.omo_ops_manager_cursor_replace.respawn_codex") as respawn,
            ):
                result = replace_manager(live_args)
            self.assertIn("tool is now cursor", result)
            respawn.assert_called_once()
            command = respawn.call_args.args[1]
            self.assertIn("agent", command)
            self.assertIn("cursor-grok-4.6-xhigh", command)
            self.assertNotIn("resume", command.casefold())
            after = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            assert after is not None
            self.assertEqual("cursor", after.tool)
            self.assertEqual(pending_before.pending_task_items, after.pending_task_items)
            self.assertEqual("wl:18", after.managerat)
            self.assertEqual("wl:3", after.runat)
            self.assertTrue(after.is_manager)
            self.assertEqual(child_before, (root / "child.md").read_text(encoding="utf-8"))

    def test_bind_refuses_same_pane_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            write_ops_root(root)
            pane = pane_for(root)
            with (
                patch("omo_manager.omo_ops_manager_cursor_replace.resolve_exact_pane", return_value=identity_for(pane)),
                patch("omo_manager.omo_ops_manager_cursor_replace.resolve_pane", return_value=pane),
                patch.dict(os.environ, {"TMUX_PANE": pane.pane_id}, clear=False),
                self.assertRaisesRegex(ReplaceError, "different pane than wl:3"),
            ):
                bind(helper_args(root, state))

    def test_cursor_command_stays_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MANAGER.md").write_text("manager\n", encoding="utf-8")
            prompt = root / "continuation.txt"
            prompt.write_text(CONTINUATION, encoding="utf-8")
            rendered = cursor_command(pane_for(root), root, prompt)
            self.assertIn("--workspace", rendered)
            self.assertIn("cursor-grok-4.6-xhigh", rendered)
            self.assertIn("OMO_AGENT_TMUX_TARGET=wl:3.0", rendered)
            self.assertNotIn("resume", rendered.casefold())


if __name__ == "__main__":
    unittest.main()
