from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager import omo_pending_watch as pending_watcher
from omo_manager.email_idle_watcher import append_pending, current_manager_file, dated_manager_file, existing_consumed_source_line, existing_source_pending_line, normalize_human_subject
from omo_manager.omo_email_subject import RecentHeader, fetch_recent_header, manager_subject_w_target, normalized_subject_key, prepare_subject, prepare_subject_and_headers, reply_headers_for_subject, strip_leading_tmux_tags
from omo_manager.omo_pending_watch import Args, find_markers

ORIGINAL_PENDING_WATCH_RUN = pending_watcher.subprocess.run


def capture_delivery_call(command: list[str]) -> list[str]:
    if command and command[0] == "omo_tmux_send.py" and "--message-file" in command:
        message_file = Path(command[command.index("--message-file") + 1])
        target = command[command.index("--target") + 1]
        captured = ["omo_tmux_send.py", message_file.read_text(encoding="utf-8"), "--manager-target", target, *command[1:]]
        if "--enter" in command:
            captured.append("--submit")
        return captured
    return command


def delivery_target(command: list[str]) -> str:
    if "--target" in command:
        return command[command.index("--target") + 1]
    return command[command.index("--manager-target") + 1]


def agent_pointer_paths(text: str) -> list[Path]:
    return [Path(match) for match in re.findall(r"^\(from agent [^ ]+ (/tmp/[^)]+)\)$", text, flags=re.MULTILINE)]


def assert_concise_agent_report(test: unittest.TestCase, report_text: str, *, agent: str, tmux: str, task_file: str, message: bytes | None = None) -> None:
    header, sep, _body = report_text.partition("message:\n")
    test.assertEqual("message:\n", sep)
    test.assertRegex(header, re.compile(rf"^\(sent from {re.escape(agent)} via omo_report\.sh tmux={re.escape(tmux)} time=\d{{2}}:\d{{2}} task-file={re.escape(task_file)}\)$", re.MULTILINE))
    if message is None:
        test.assertIn("[message-sha256: ", header)
    else:
        test.assertIn(f"[message-sha256: {hashlib.sha256(message).hexdigest()}]", header)
    test.assertNotIn("[omo-message-source: ", header)
    test.assertNotIn("(from agent ", header)
    test.assertNotIn("(report manager ", header)
    test.assertNotIn("report-file=", header)
    test.assertNotIn("message-file: ", header)
    test.assertNotIn("task-pointer: ", header)
    test.assertNotIn("tmux_pane_id=", header)
    test.assertNotIn("tmux_window_name=", header)


def task_frontmatter(
    status: str = "running",
    runat: str = "wl:2",
    managerat: str = "wl:1",
    *,
    is_manager: bool = False,
    blocked_on: str = "",
    pending_items: tuple[str, ...] = (),
) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
    ]
    if blocked_on:
        lines.append(f"blocked_on: {blocked_on}")
    lines.extend(
        [
            f"runat: {runat}",
            "tool: codex",
            f"managerat: {managerat}",
            f"is_manager: {str(is_manager).lower()}",
        ]
    )
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.append("---")
    return "\n".join(lines) + "\n"


def report_test_env(tmp: str, manager_target: str = "main:0.0", **updates: str) -> dict[str, str]:
    root = Path(tmp) / "logs"
    local_env = Path(tmp) / "local.env"
    local_env.write_text(
        "\n".join(
            [
                f"OMO_WORK_LOGS_ROOT={shlex.quote(str(root))}",
                f"OMO_MANAGER_TMUX_TARGET={shlex.quote(manager_target)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bin_dir = Path(tmp) / "bin"
    bin_dir.mkdir(exist_ok=True)
    tmux = bin_dir / "tmux"
    if not tmux.exists():
        tmux.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ -n "${OMO_FAKE_TMUX_INFO:-}" ]; then
  printf '%b\n' "$OMO_FAKE_TMUX_INFO"
else
  printf 'agent\t1\t0\t%%report\treport-window\n'
fi
""",
            encoding="utf-8",
        )
        tmux.chmod(0o700)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TMUX", "TMUX_PANE", "OMO_MANAGER_LOCAL_ENV", "OMO_WORK_LOGS_ROOT", "OMO_MANAGER_TMUX_TARGET", "OMO_MANAGER_URL"}
    }
    env.update(
        {
            "OMO_MANAGER_LOCAL_ENV": str(local_env),
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "TMUX_PANE": "%report",
        }
    )
    env.update(updates)
    return env


def omo_report_script() -> str:
    return str(Path.home() / ".config/omo_manager/omo_report.sh")


def omo_report_command(*, status: str = "done", agent: str = "agent-4002", message_file: Path) -> list[str]:
    return [
        omo_report_script(),
        "--status",
        status,
        "--agent",
        agent,
        "--message-file",
        str(message_file),
    ]


def omo_report_alloc_command() -> list[str]:
    return [omo_report_script(), "--alloc-message-file"]


def report_ref(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def add_report_todo_ref(root: Path, path: Path) -> None:
    ref = report_ref(root, path)
    todo = root / "TODO.md"
    if todo.exists():
        text = todo.read_text(encoding="utf-8")
        if ref in {line.strip().strip("`") for line in text.splitlines()}:
            return
        if not text.endswith("\n"):
            text += "\n"
    else:
        text = "current:\n"
    todo.write_text(f"{text}{ref}\n", encoding="utf-8")


def write_report_todo(root: Path, *paths: Path) -> None:
    lines = ["current:", *(report_ref(root, path) for path in paths)]
    (root / "TODO.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report_worker_task(root: Path, name: str = "task.md", *, runat: str = "agent:1", managerat: str = "main:0.0") -> Path:
    path = root / name
    path.write_text(task_frontmatter(runat=runat, managerat=managerat), encoding="utf-8")
    add_report_todo_ref(root, path)
    return path


class PendingMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._send_patch = patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=self._fake_send_to_codex)
        self._send_patch.start()
        self.addCleanup(self._send_patch.stop)

    def _fake_send_to_codex(self, target: str, message: str, _options: object = None, **_kwargs: object) -> None:
        if pending_watcher.subprocess.run is ORIGINAL_PENDING_WATCH_RUN:
            return
        command = ["omo_tmux_send.py", message, "--manager-target", target, "--target", target, "--submit"]
        result = pending_watcher.subprocess.run(command, check=False)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, command)

    def test_email_pending_block_has_single_source_marker_and_is_not_generic_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            line = append_pending(root, mail)
            self.assertEqual(line, existing_source_pending_line(root, mail))
            path = dated_manager_file(root)
            text = path.read_text(encoding="utf-8")
            self.assertIn("(record and delegate manager_mail/4002.txt)", text)
            self.assertNotIn("[source: email manager_mail/4002.txt]", text)
            self.assertNotIn("[summary: human reply to manager]", text)
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("human", markers[0].origin)
            self.assertEqual("email", markers[0].source)
            self.assertIn("origin=human source=email action=ack-human", markers[0].ref)
            self.assertIn("(delegate manager_mail/4002.txt)", markers[0].ref)
            self.assertNotIn("(record and delegate manager_mail/4002.txt)", markers[0].ref)

    def test_manual_pending_block_is_human_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            path.write_text("(pending)\nplease handle this\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("human", markers[0].origin)
            self.assertEqual("manual", markers[0].source)
            self.assertEqual("ack-human", markers[0].action)

    def test_pending_marker_carries_latest_blocked_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(f"{task_frontmatter(status='blocked', blocked_on='waiting on API approval')}notes\n(pending)\nplease route\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("waiting on API approval", markers[0].blocked_reason)
            self.assertIn("blocked-context: latest prior status is blocked; reason=waiting on API approval", markers[0].ref)

    def test_pending_marker_uses_nearest_prior_status_for_blocked_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(f"{task_frontmatter(status='running')}notes\n(pending)\nplease route\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("", markers[0].blocked_reason)
            self.assertNotIn("blocked-context:", markers[0].ref)

    def test_pending_marker_carries_blocked_status_without_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path = root / "work_manager.md"
            path.write_text("(blocked)\n(pending)\nplease route\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("blocked with no reason in latest status line", markers[0].blocked_reason)
            self.assertIn("blocked-context: latest prior status is blocked; reason=blocked with no reason in latest status line", markers[0].ref)

    def test_pending_marker_uses_frontmatter_blocked_on_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(f"{task_frontmatter(status='blocked', runat='wl:2', managerat='wl:1', blocked_on='waiting on API approval')}\n(pending)\nplease route\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("waiting on API approval", markers[0].blocked_reason)
            self.assertIn("blocked-context: latest prior status is blocked; reason=waiting on API approval", markers[0].ref)

    def test_pending_push_includes_blocked_context(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(f"{task_frontmatter(status='blocked', blocked_on='waiting on API approval')}\n(pending)\nplease route\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("<snippet file=\"task.md:12-13\">", text)
            self.assertIn("<blocked_on>waiting on API approval</blocked_on>", text)

    def test_pending_push_uses_managerat_target_for_worker_task_file(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\nplease route\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:1", calls[0][calls[0].index("--manager-target") + 1])

    def test_pending_push_uses_runat_for_manager_task_frontmatter(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "submanager.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1', is_manager=True)}\n(pending)\nplease route\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:2", calls[0][calls[0].index("--manager-target") + 1])

    def test_for_manager_prefix_routes_manager_task_to_managerat(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "submanager.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1', is_manager=True)}\n(pending)\n\"FoR MaNaGeR\": please route upward\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:1", calls[0][calls[0].index("--manager-target") + 1])

    def test_for_manager_suffix_routes_manager_task_to_managerat(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "submanager.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1', is_manager=True)}\n(pending)\nplease route upward\n(for manager) \n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:1", calls[0][calls[0].index("--manager-target") + 1])

    def test_quoted_for_manager_marker_does_not_route_manager_task_to_managerat(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "submanager.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1', is_manager=True)}\n(pending)\n> for manager\nplease handle locally\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:2", calls[0][calls[0].index("--manager-target") + 1])

    def test_linked_file_for_manager_routes_manager_task_to_managerat(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("Please route this to the parent manager. FOR MANAGER!!!\n", encoding="utf-8")
            path = root / "submanager.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1', is_manager=True)}\n(pending)\ndocs/request.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:1", calls[0][calls[0].index("--manager-target") + 1])
            self.assertIn('<snippet file="docs/request.md:1-1">', calls[0][1])

    def test_for_manager_marker_overrides_dm_in_manager_task(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "submanager.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1', is_manager=True)}\n(pending)\nDM\nfor manager\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual(["wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertNotIn("Direct message from the human", calls[0][1])

    def test_pending_push_uses_frontmatter_managerat_over_body_managerat(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}managerat: wl:9\n(done: old route)\n\n(pending)\nplease route\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:1", calls[0][calls[0].index("--manager-target") + 1])

    def test_pending_push_ignores_managerat_after_pending(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\nplease route\nmanagerat: wl:9\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("wl:1", calls[0][calls[0].index("--manager-target") + 1])

    def test_pending_push_keeps_default_target_without_managerat(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path = root / "work_manager.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("main:0.0", calls[0][calls[0].index("--manager-target") + 1])

    def test_vl_pending_push_uses_frontmatter_manager_target(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "vl_worker.md"
            path.write_text(f"{task_frontmatter(runat='vl:1', managerat='vl:15')}\n(pending)\nplease route\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl 15\nvl_worker.md vl 1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertEqual("vl:15", calls[0][calls[0].index("--manager-target") + 1])

    def test_for_manager_pending_escalates_to_main_when_manager_target_is_not_codex(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "vl_vlhsplit_10046.md"
            path.write_text(
                f"{task_frontmatter(status='done', runat='vl:37', managerat='vl:32')}\n"
                "(pending)\n"
                "(for manager: Is the public repo artifact superseded?)\n",
                encoding="utf-8",
            )
            calls: list[tuple[str, str]] = []

            def fake_send_to_codex(target: str, message: str, _options: object = None, **_kwargs: object) -> None:
                calls.append((target, message))
                if target == "vl:32":
                    raise RuntimeError("target is not a Codex pane: vl:32")

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="wl:1.0"
            )
            with patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=fake_send_to_codex):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["vl:32", "wl:1.0"], [target for target, _message in calls])
            escalated = calls[1][1]
            self.assertTrue(escalated.startswith("Normally record pending items and remove the consumed `(pending)` marker by running:"))
            self.assertIn("omo_record_pending.py", escalated)
            self.assertIn("--ack-human", escalated)
            self.assertIn("Delivery to resolved target `vl:32` failed: target is not a Codex pane: vl:32.", escalated)
            self.assertIn("<snippet file=\"vl_vlhsplit_10046.md:11-12\">", escalated)

    def test_target_unavailable_matches_codex_status_not_codex_error(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        self.assertTrue(watcher.target_unavailable(watcher.DeliveryResult(1, "target left supported Codex state before submit: vl:32 status=not_codex")))

    def test_agent_report_in_vl_submanager_file_routes_to_submanager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "vl_submanager_current_8653.md"
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n", encoding="utf-8")
            _ = task.write_text(
                f"{task_frontmatter(runat='vl:15', managerat='wl:16.0', is_manager=True)}(pending)\n(from agent vl:15 /tmp/report.md)\n",
                encoding="utf-8",
            )
            markers = find_markers(root, [task])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            self.assertEqual("vl:15", watcher.marker_manager_target(args, markers[0]))

    def test_human_pending_in_vl_submanager_file_still_routes_to_submanager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "vl_submanager_current_8653.md"
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n", encoding="utf-8")
            _ = task.write_text(f"{task_frontmatter(runat='vl:15', managerat='wl:16.0', is_manager=True)}(pending)\nplease handle this locally\n", encoding="utf-8")
            markers = find_markers(root, [task])
            self.assertEqual(1, len(markers))
            self.assertEqual("human", markers[0].origin)
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            self.assertEqual("vl:15", watcher.marker_manager_target(args, markers[0]))

    def test_agent_source_marker_is_agent_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(
                "(pending)\n"
                "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done tmux_pane=%22]\n"
                "(from agent agent-4002 via omo_report.sh status=done)\n",
                encoding="utf-8",
            )
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)
            self.assertEqual("agent", markers[0].source)
            self.assertEqual("no-human-ack", markers[0].action)

    def test_agent_source_marker_later_in_pending_block_is_agent_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(
                "(pending)\n"
                "(report manager 2026-06-10 12:02 agent=agent-4002 status=done)\n"
                "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done]\n",
                encoding="utf-8",
            )
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)
            self.assertEqual("no-human-ack", markers[0].action)

    def test_manager_agent_problem_pending_block_is_agent_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            path.write_text(
                "(pending)\n"
                "(from agent omo_pending_watch agent-problem)\n"
                "manager agent problem: running task marker needs attention.\n"
                "agent-problems: stuck_input=1\n",
                encoding="utf-8",
            )
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)
            self.assertEqual("agent", markers[0].source)
            self.assertEqual("no-human-ack", markers[0].action)

    def test_email_pending_block_uses_configured_active_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active_log = root / "work_manager_20260527.md"
            mail = root / "manager_mail" / "4333.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            line = append_pending(root, mail, active_log)
            self.assertEqual(line, existing_source_pending_line(root, mail, active_log))
            self.assertFalse((root / "work_manager.md").exists())
            text = active_log.read_text(encoding="utf-8")
            self.assertIn("(record and delegate manager_mail/4333.txt)", text)
            markers = find_markers(root, [active_log])
            self.assertEqual(1, len(markers))

    def test_email_append_pending_suppresses_acknowledged_source_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "work_manager_today.md"
            mail = root / "manager_mail" / "8867.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text("(done: acknowledged manager_mail/8867.txt)\n", encoding="utf-8")

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            text = manager_file.read_text(encoding="utf-8")
            self.assertNotIn("(pending)", text)
            self.assertNotIn("(record and delegate manager_mail/8867.txt)", text)

    def test_email_append_pending_suppresses_routed_source_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "work_manager_today.md"
            mail = root / "manager_mail" / "8867.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text("(manager routed: manager_mail/8867.txt to wl:16)\n", encoding="utf-8")

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            text = manager_file.read_text(encoding="utf-8")
            self.assertNotIn("(pending)", text)
            self.assertEqual(1, text.count("manager_mail/8867.txt"))

    def test_email_append_pending_suppresses_stale_marker_cleanup_comment(self) -> None:
        for uid in ("9460", "9507"):
            with self.subTest(uid=uid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / f"{uid}.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(
                    "(comment: Acknowledged stale pending notice for line 1061 by email with subject `VL duplicate helper notice cleared`; "
                    "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9497.txt`, and `manager_mail/9507.txt`. "
                    "No new VL work was represented by this batch.)\n",
                    encoding="utf-8",
                )

                line = append_pending(root, mail, manager_file)

                self.assertEqual(1, line)
                self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
                text = manager_file.read_text(encoding="utf-8")
                self.assertNotIn("(pending)", text)
                self.assertNotIn(f"(record and delegate manager_mail/{uid}.txt)", text)

    def test_email_append_pending_keeps_unconsumed_comment_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: `manager_mail/9507.txt` still needs work; previous `manager_mail/9497.txt` cleared.)\n"
                "(comment: `manager_mail/9507.txt` is unhandled.)\n"
                "(comment: `manager_mail/9507.txt` was not routed; keep pending.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(5, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))
            text = manager_file.read_text(encoding="utf-8")
            self.assertIn("(pending)", text)
            self.assertIn("(record and delegate manager_mail/9507.txt)", text)

    def test_email_consumed_comment_matching_is_scoped_to_same_mail_clause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: `manager_mail/9507.txt` is still pending; removed repeated watcher markers for `manager_mail/9497.txt`.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(3, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_comment_ignores_other_mail_negative_clause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: acknowledged `manager_mail/9507.txt`; `manager_mail/9497.txt` still needs work.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_accepts_removed_duplicate_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: removed duplicate watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt`.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_accepts_repeated_stale_watcher_markers_and_batch(self) -> None:
        for text in (
            "Removed repeated stale watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt`",
            "Removed another repeated stale watcher batch for `manager_mail/9460.txt`, `manager_mail/9507.txt`",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9507.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"(comment: {text}.)\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(1, line)
                self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
                self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_does_not_apply_other_mail_cleanup_to_active_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: active mail `manager_mail/9507.txt` is a follow-up and removed repeated watcher markers for `manager_mail/9497.txt`.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(3, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))
            text = manager_file.read_text(encoding="utf-8")
            self.assertIn("(pending)", text)
            self.assertIn("(record and delegate manager_mail/9507.txt)", text)

    def test_email_consumed_comment_rejects_other_cleanup_before_target_status(self) -> None:
        for text in (
            "removed repeated watcher markers for `manager_mail/9497.txt`, and still pending `manager_mail/9507.txt`",
            "removed repeated watcher markers for `manager_mail/9497.txt`, not removed `manager_mail/9507.txt`",
            "removed repeated watcher markers for `manager_mail/9497.txt`, not cleared `manager_mail/9507.txt`",
            "removed repeated watcher markers for `manager_mail/9497.txt`, not handled `manager_mail/9507.txt`",
            "removed repeated watcher markers for `manager_mail/9497.txt`, not routed `manager_mail/9507.txt`",
            "removed repeated watcher markers for `manager_mail/9497.txt`, then deferred `manager_mail/9507.txt`",
            "removed repeated watcher markers for `manager_mail/9497.txt`, later noted `manager_mail/9507.txt` separately",
            "removed repeated watcher markers for `manager_mail/9497.txt`, and mentioned `manager_mail/9507.txt` separately",
            "removed repeated watcher markers for `manager_mail/9497.txt`, and logged `manager_mail/9507.txt` separately",
            "removed repeated watcher markers for `manager_mail/9497.txt`, and `manager_mail/9507.txt` was logged separately",
            "removed repeated watcher markers for `manager_mail/9497.txt`, and `manager_mail/9507.txt` is separately logged",
            "removed repeated watcher markers for `manager_mail/9497.txt`, and `manager_mail/9507.txt` not active",
            "removed repeated watcher markers for `manager_mail/9497.txt`, and `manager_mail/9507.txt` no longer pending",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9507.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"(comment: {text}.)\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(3, line)
                self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))
                self.assertIn("(record and delegate manager_mail/9507.txt)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_rejects_trailing_status_for_earlier_cleanup_ref(self) -> None:
        for text in (
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt`; still pending",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt`; active follow-up remains",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt`. Still pending",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt`. Active follow-up remains",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt` and still pending",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt` and active follow-up remains",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9460.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"(comment: {text}.)\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(3, line)
                self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))
                self.assertIn("(record and delegate manager_mail/9460.txt)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_accepts_multiref_cleanup_with_inactive_suffix_for_earlier_ref(self) -> None:
        for text in (
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt` and no pending work remains",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt` and not active",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt` and no longer pending",
            "removed repeated watcher markers for `manager_mail/9460.txt`, `manager_mail/9507.txt` and not a follow-up",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9460.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"(comment: {text}.)\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(1, line)
                self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
                self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_accepts_cleanup_with_no_live_mail_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: removed repeated watcher markers for `manager_mail/9507.txt`; no live mail remains.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_rejects_not_yet_consumed_words(self) -> None:
        for verb in ("acknowledged", "routed", "handled", "removed", "cleared"):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9507.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"(comment: not yet {verb} `manager_mail/9507.txt`.)\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(3, line)
                self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_comment_rejects_negated_consumed_forms(self) -> None:
        for text in (
            "has not been consumed `manager_mail/9507.txt`",
            "wasn't consumed `manager_mail/9507.txt`",
            "not-yet consumed `manager_mail/9507.txt`",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9507.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"(comment: {text}.)\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(3, line)
                self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_comment_splits_sentences_without_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: removed duplicate watcher markers for `manager_mail/9497.txt`.Active mail `manager_mail/9507.txt` is live.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(3, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_comment_requires_exact_mail_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text("(comment: consumed duplicate pending marker for `manager_mail/9507.txt.bak`.)\n", encoding="utf-8")

            line = append_pending(root, mail, manager_file)

            self.assertEqual(3, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_status_lines_require_exact_mail_ref(self) -> None:
        for text in (
            "(done: archived manager_mail/9507.txt.bak)",
            "(running: manager_mail/9507.txt2)",
            "(manager routed: manager_mail/9507.txt.bak to wl:16)",
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9507.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"{text}\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(3, line)
                self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_status_line_accepts_unquoted_sentence_final_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text("(done: acknowledged manager_mail/9507.txt.)\n", encoding="utf-8")

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_does_not_consume_active_mail_after_other_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: removed repeated watcher markers for `manager_mail/9497.txt` and active mail `manager_mail/9507.txt` is live.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(3, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_comment_does_not_consume_active_mail_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: active mail `manager_mail/9507.txt` is live; removed repeated watcher markers for `manager_mail/9497.txt`, `manager_mail/9507.txt`.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(3, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_comment_does_not_consume_live_mail_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(comment: live mail manager_mail/9507.txt; removed repeated watcher markers for manager_mail/9497.txt, manager_mail/9507.txt.)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertEqual(3, line)
            self.assertIsNone(existing_consumed_source_line(root, mail, manager_file))

    def test_email_consumed_comment_accepts_cleared_stale_duplicate_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text("(comment: cleared stale duplicate pending markers for `manager_mail/9507.txt`.)\n", encoding="utf-8")

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_accepts_consumed_duplicate_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "9507.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text("(comment: Consumed duplicate pending marker for `manager_mail/9507.txt`.)\n", encoding="utf-8")

            line = append_pending(root, mail, manager_file)

            self.assertEqual(1, line)
            self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
            self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_consumed_comment_accepts_direct_handled_routed_removed_cleared(self) -> None:
        for verb in ("handled", "routed", "removed", "cleared"):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager_file = root / "vl_submanager_current_8653.md"
                mail = root / "manager_mail" / "9507.txt"
                mail.parent.mkdir()
                _ = mail.write_text("body\n", encoding="utf-8")
                manager_file.write_text(f"(comment: {verb} `manager_mail/9507.txt`.)\n", encoding="utf-8")

                line = append_pending(root, mail, manager_file)

                self.assertEqual(1, line)
                self.assertEqual(1, existing_consumed_source_line(root, mail, manager_file))
                self.assertNotIn("(pending)", manager_file.read_text(encoding="utf-8"))

    def test_email_append_pending_suppresses_legacy_routed_prose_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_file = root / "work_manager_today.md"
            mail = root / "manager_mail" / "4480.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            manager_file.write_text(
                "(pending)\n"
                "(manager routed: to `task.md`.)\n"
                "(from email manager_mail/4480.txt)\n",
                encoding="utf-8",
            )

            line = append_pending(root, mail, manager_file)

            self.assertIsNone(existing_source_pending_line(root, mail, manager_file))
            self.assertEqual(3, line)
            self.assertEqual(3, existing_consumed_source_line(root, mail, manager_file))
            text = manager_file.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("(pending)"))
            self.assertEqual(1, text.count("manager_mail/4480.txt"))

    def test_submanager_email_pending_block_routes_to_task_file(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "submanager.md"
            mail = root / "manager_mail" / "5001.txt"
            mail.parent.mkdir()
            _ = task.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\n", encoding="utf-8")
            _ = mail.write_text("body\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] [wl:1] manager update")
            line = watcher.append_pending(root, mail, route.manager_file, route.routed_target)
            text = task.read_text(encoding="utf-8")
            self.assertEqual(task, route.manager_file)
            self.assertEqual("wl:1", route.manager_target)
            self.assertEqual(11, line)
            self.assertIn("(manager routed: wl:1)", text)
            self.assertIn("(record and delegate manager_mail/5001.txt)", text)
            self.assertEqual([], find_markers(root, [task]))
            self.assertEqual(line, watcher.existing_source_pending_line(root, mail, task))

    def test_numeric_submanager_email_subject_uses_default_route(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "vl_submanager_current_8653.md"
            mail = root / "manager_mail" / "8746.txt"
            mail.parent.mkdir()
            _ = task.write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n", encoding="utf-8")
            _ = mail.write_text("body\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="wl:16.0")
            route = watcher.email_route(args, "Re: [a] [15] VL follow-up")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("wl:16.0", route.manager_target)

    def test_normalized_numeric_submanager_email_subject_uses_default_route(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "vl_submanager_current_8653.md"
            _ = task.write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="wl:16.0")
            route = watcher.email_route(args, "Re: 15 VL follow-up")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("wl:16.0", route.manager_target)

    def test_submanager_email_subject_pane_target_matches_window_runat(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "submanager.md"
            _ = task.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] [wl:1.1] manager update")
            self.assertEqual(task, route.manager_file)
            self.assertEqual("wl:1.1", route.manager_target)
            self.assertEqual("wl:1.1", route.routed_target)

    def test_half_bracketed_submanager_email_subject_uses_default_route(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "submanager.md"
            _ = task.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            for subject in ("Re: [a] [wl:1 manager update", "Re: [a] wl:1] manager update"):
                route = watcher.email_route(args, subject)
                self.assertEqual(root / "work_manager_today.md", route.manager_file)
                self.assertEqual("main:0.0", route.manager_target)

    def test_recovery_email_human_passes_sender_tmux_target(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        calls: list[list[str]] = []

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(capture_delivery_call(command))
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="wl:16.0")
            old_run = watcher.subprocess.run
            watcher.subprocess.run = run
            try:
                watcher.email_human(args, "[a] Recovery action needed", "body\n")
            finally:
                watcher.subprocess.run = old_run
        self.assertEqual("wl:16.0", calls[0][calls[0].index("--sender-tmux-target") + 1])

    def test_legacy_managerat_does_not_route_addressed_worker_mail_to_submanager(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submanager = root / "submanager.md"
            worker = root / "worker.md"
            _ = submanager.write_text("runat: wl:1 pcodx\n", encoding="utf-8")
            _ = worker.write_text("runat: wl:2 codex\nmanagerat: wl:1\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\nworker.md wl 2\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] [wl:2] manager update")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("main:0.0", route.manager_target)
            self.assertEqual("", route.routed_target)

    def test_frontmatter_managerat_routes_addressed_worker_mail_to_submanager(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submanager = root / "submanager.md"
            worker = root / "worker.md"
            _ = submanager.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\nworker.md wl 2\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] [wl:2] manager update")
            self.assertEqual(submanager, route.manager_file)
            self.assertEqual("wl:1", route.manager_target)
            self.assertEqual("wl:1", route.routed_target)

    def test_managerat_routes_inactive_addressed_worker_mail_to_active_submanager(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submanager = root / "vl_submanager_current_8653.md"
            worker = root / "vl_late_walk_8951.md"
            _ = submanager.write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="vl:17", managerat="vl:15"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="wl:16.0")
            route = watcher.email_route(args, "Re: [a] [vl:17] Later VL failures were more subtle")
            self.assertEqual(submanager, route.manager_file)
            self.assertEqual("vl:15", route.manager_target)
            self.assertEqual("vl:15", route.routed_target)

    def test_managerat_ignores_inactive_worker_owner_only_in_previous_todo(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = root / "old_owner.md"
            worker = root / "old_worker.md"
            _ = owner.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n\nprevious:\nold_owner.md wl:1 (done)\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] [wl:2] manager update")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("main:0.0", route.manager_target)
            self.assertEqual("", route.routed_target)

    def test_managerat_ignores_previous_todo_worker_and_owner(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = root / "old_owner.md"
            worker = root / "old_worker.md"
            _ = owner.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n\nprevious:\nold_worker.md wl:2 (done)\nold_owner.md wl:1 (done)\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] [wl:2] manager update")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("main:0.0", route.manager_target)
            self.assertEqual("", route.routed_target)

    def test_managerat_ignores_inactive_worker_owned_by_current_watcher_without_current_owner(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "old_worker.md"
            _ = worker.write_text(task_frontmatter(runat="vl:17", managerat="vl:15"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="vl:15")
            route = watcher.email_route(args, "Re: [a] [vl:17] Later VL failures were more subtle")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("vl:15", route.manager_target)
            self.assertEqual("", route.routed_target)

    def test_managerat_routes_inactive_worker_when_unowned_stale_file_shares_target(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submanager = root / "vl_submanager_current_8653.md"
            stale = root / "newer_unowned.md"
            worker = root / "vl_late_walk_8951.md"
            _ = submanager.write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="vl:17", managerat="vl:15"), encoding="utf-8")
            _ = stale.write_text("runat: vl:17 codex\n", encoding="utf-8")
            os.utime(worker, (1, 1))
            os.utime(stale, (2, 2))
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="wl:16.0")
            route = watcher.email_route(args, "Re: [a] [vl:17] Later VL failures were more subtle")
            self.assertEqual(submanager, route.manager_file)
            self.assertEqual("vl:15", route.manager_target)
            self.assertEqual("vl:15", route.routed_target)

    def test_managerat_routes_human_pending_worker_to_current_owner(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submanager = root / "vl_submanager_current_8653.md"
            worker = root / "vl_late_walk_8951.md"
            _ = submanager.write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="vl:17", managerat="vl:15"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n\nhuman pending:\nvl_late_walk_8951.md vl:17\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="wl:16.0")
            route = watcher.email_route(args, "Re: [a] [vl:17] Later VL failures were more subtle")
            self.assertEqual(submanager, route.manager_file)
            self.assertEqual("vl:15", route.manager_target)
            self.assertEqual("vl:15", route.routed_target)

    def test_email_watcher_dispatches_targeted_reply_to_task_file(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            submanager = root / "submanager.md"
            worker = root / "worker.md"
            _ = submanager.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\nworker.md wl 2\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] [wl:2] manager update"
            msg.set_content("body\n")
            pushes: list[tuple[Path | None, str, int]] = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"22"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref

            def push(push_args: watcher.Args, line_no: int) -> bool:
                pushes.append((push_args.manager_file, push_args.manager_target, line_no))
                return True

            watcher.push_email_ref = push
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="main:0.0")
                watcher.handle_unseen(Client(), args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual([(submanager, "wl:1", 11)], pushes)
            self.assertIn("(manager routed: wl:1)", submanager.read_text(encoding="utf-8"))
            self.assertFalse((root / "work_manager_today.md").exists())

    def test_email_watcher_direct_dm_reply_goes_to_worker_task_file(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            submanager = root / "submanager.md"
            worker = root / "worker.md"
            _ = submanager.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\nworker.md wl 2\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] [wl:2] manager update"
            msg.set_content("Please handle this directly. DM only\n")
            pushes: list[tuple[Path | None, str, int]] = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"22"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref

            def push(push_args: watcher.Args, line_no: int) -> bool:
                pushes.append((push_args.manager_file, push_args.manager_target, line_no))
                return True

            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="main:0.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual([], pushes)
            self.assertIn("(pending)\n(record and delegate manager_mail/22.txt)\n", worker.read_text(encoding="utf-8"))
            self.assertNotIn("manager_mail/22.txt", submanager.read_text(encoding="utf-8"))
            self.assertEqual([("22", "+FLAGS", r"(\Seen)")], client.stores)
            self.assertFalse((root / "work_manager_today.md").exists())
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            pending_args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(pending_watcher.scan_once(pending_args, {}, [worker]))
            self.assertEqual(["wl:2"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertIn("Please handle this directly.", calls[0][1])
            self.assertNotIn("DM only", calls[0][1])
            self.assertNotIn("(pending)", calls[0][1])
            self.assertNotIn("(pending)", worker.read_text(encoding="utf-8"))
            processed_path = state / "email-processed-uids.tsv"
            processed_path.unlink(missing_ok=True)

            class RecoveryClient:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"22"]
                    if command == "fetch":
                        raise AssertionError("consumed direct DM-only source should not be refetched")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            watcher.handle_unseen(RecoveryClient(), args)
            self.assertEqual(1, worker.read_text(encoding="utf-8").count("manager_mail/22.txt"))
            self.assertNotIn("(pending)", worker.read_text(encoding="utf-8"))

    def test_email_watcher_direct_dm_reply_copies_worker_manager(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            submanager = root / "submanager.md"
            worker = root / "worker.md"
            _ = submanager.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\nworker.md wl 2\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] [wl:2] manager update"
            msg.set_content("Please handle this directly. DM\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"23"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            pushes: list[tuple[Path | None, str, int]] = []
            old_push = watcher.push_email_ref

            def push(push_args: watcher.Args, line_no: int) -> bool:
                pushes.append((push_args.manager_file, push_args.manager_target, line_no))
                return True

            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="main:0.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual([], pushes)
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            pending_args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(pending_watcher.scan_once(pending_args, {}, [worker]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertIn("Please handle this directly.", calls[0][1])
            self.assertNotIn("DM\n", calls[0][1])
            self.assertIn("this message is already dispatched to the agent, this is FYI", calls[1][1])
            self.assertIn("--ack-human", calls[1][1])
            self.assertIn("--email-file manager_mail/23.txt", calls[1][1])

    def test_email_watcher_leading_dm_reply_goes_to_worker_task_file(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submanager = root / "submanager.md"
            worker = root / "worker.md"
            _ = submanager.write_text(task_frontmatter(runat="wl:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsubmanager.md wl 1\nworker.md wl 2\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] [wl:2] manager update", "DM: please handle this directly.")
            self.assertEqual(worker, route.manager_file)
            self.assertEqual("wl:2", route.manager_target)
            self.assertTrue(route.direct_delivery)

    def test_managerat_can_route_addressed_worker_mail_to_main_manager(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "worker.md"
            other = root / "other.md"
            _ = worker.write_text(task_frontmatter(runat="wl:2", managerat="main:0.0"), encoding="utf-8")
            _ = other.write_text(task_frontmatter(runat="main:0.0", managerat="wl:1"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md wl 2\nother.md main 0\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] wl:2 manager update")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("main:0.0", route.manager_target)
            self.assertEqual("main:0.0", route.routed_target)

    def test_email_retry_push_without_frontmatter_uses_default_manager_target(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "submanager.md"
            _ = task.write_text("runat: wl:1 pcodx\n\n(pending)\n(manager routed: wl:1)\n(from email manager_mail/5002.txt)\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            retry_args = watcher.args_for_manager_file(args, task)
            self.assertEqual("main:0.0", retry_args.manager_target)
            self.assertEqual(task, retry_args.manager_file)

    def test_email_retry_push_uses_worker_managerat_not_runat(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "worker.md"
            _ = task.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(from agent wl:2 /tmp/report.md)\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            retry_args = watcher.args_for_manager_file(args, task)
            self.assertEqual("wl:1", retry_args.manager_target)
            self.assertEqual(task, retry_args.manager_file)

    def test_submanager_email_retry_push_uses_frontmatter_manager_target(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "submanager.md"
            _ = task.write_text(f"{task_frontmatter(runat='wl:1', managerat='main:0.0', is_manager=True)}\n(pending)\n(manager routed: wl:1)\n(from email manager_mail/5002.txt)\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            retry_args = watcher.args_for_manager_file(args, task)
            self.assertEqual("wl:1", retry_args.manager_target)
            self.assertEqual(task, retry_args.manager_file)

    def test_submanager_email_retry_prefers_recorded_routed_target(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "submanager.md"
            _ = task.write_text("runat: wl:9 pcodx\n\nrunat: wl:1 pcodx\n\n(pending)\n(manager routed: wl:1)\n(from email manager_mail/5002.txt)\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            retry_args = watcher.args_for_manager_file(args, task, 5)
            self.assertEqual("wl:1", retry_args.manager_target)
            self.assertEqual(task, retry_args.manager_file)

    def test_submanager_email_retry_ignores_legacy_routed_prose_and_runat(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "submanager.md"
            _ = task.write_text("runat: wl:1 pcodx\n\n(pending)\n(manager routed: to `task.md`.)\n(from email manager_mail/5002.txt)\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            retry_args = watcher.args_for_manager_file(args, task, 3)
            self.assertEqual("main:0.0", retry_args.manager_target)
            self.assertEqual(task, retry_args.manager_file)

    def test_submanager_email_route_ignores_absolute_todo_path_outside_root(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            outside = Path(tmp) / "outside.md"
            root.mkdir()
            _ = outside.write_text("runat: wl:1 pcodx\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text(f"current:\n{outside} wl 1\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] wl:1 manager update")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("main:0.0", route.manager_target)
            self.assertEqual("", route.routed_target)

    def test_submanager_email_route_ignores_non_todo_stale_task_file(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "old_submanager.md"
            _ = stale.write_text("runat: wl:1 pcodx\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] wl:1 manager update")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("main:0.0", route.manager_target)
            self.assertEqual("", route.routed_target)

    def test_submanager_email_route_ignores_non_todo_worker_without_active_owner(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "old_worker.md"
            _ = stale.write_text("runat: wl:2 codex\nmanagerat: wl:1\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            args = watcher.Args(root, "", root / "manager_mail", root / "state", root / "work_manager_today.md", True, "self@example.test", 900, Path("/bin/false"), manager_target="main:0.0")
            route = watcher.email_route(args, "Re: [a] wl:2 manager update")
            self.assertEqual(root / "work_manager_today.md", route.manager_file)
            self.assertEqual("main:0.0", route.manager_target)
            self.assertEqual("", route.routed_target)

    def test_unaccepted_retry_ignores_non_todo_stale_pending_source(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "old_submanager.md"
            mail = root / "manager_mail" / "5003.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            _ = stale.write_text("runat: wl:1 pcodx\n\n(pending)\n(manager routed: wl:1)\n(from email manager_mail/5003.txt)\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            self.assertIsNone(watcher.existing_source_pending_path_line_in_root(root, mail, root / "work_manager_today.md"))

    def test_email_watcher_idle_wait_is_configurable(self) -> None:
        from omo_manager.email_idle_watcher import parse_args

        args = parse_args(["--root", "/tmp/root", "--idle-wait-s", "7.5", "--once"])
        self.assertEqual(7.5, args.idle_wait_s)

    def test_email_watcher_imap_timeout_is_configurable(self) -> None:
        from omo_manager.email_idle_watcher import parse_args

        args = parse_args(["--root", "/tmp/root", "--imap-timeout-s", "8.5", "--once"])
        self.assertEqual(8.5, args.imap_timeout_s)

    def test_email_watcher_idle_eof_raises_instead_of_spinning(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Socket:
            timeout_s: float | None = None

            def gettimeout(self) -> float | None:
                return self.timeout_s

            def settimeout(self, timeout_s: float | None) -> None:
                self.timeout_s = timeout_s

        class Client:
            sent: list[bytes] = []
            sock = Socket()

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def socket(self) -> Socket:
                return self.sock

            def readline(self) -> bytes:
                return b""

        with self.assertRaises(ConnectionError):
            watcher.idle_once(Client(), 0)

    def test_email_watcher_idle_start_read_timeout_raises_and_restores_socket_timeout(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Socket:
            timeout_s: float | None = 42.0

            def gettimeout(self) -> float | None:
                return self.timeout_s

            def settimeout(self, timeout_s: float | None) -> None:
                self.timeout_s = timeout_s

        class Client:
            sent: list[bytes] = []
            sock = Socket()

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def socket(self) -> Socket:
                return self.sock

            def readline(self) -> bytes:
                raise TimeoutError("socket timed out")

        client = Client()
        with self.assertRaises(TimeoutError):
            watcher.idle_once(client, 0)
        self.assertEqual(42.0, client.sock.timeout_s)

    def test_email_watcher_idle_done_reads_buffered_completion(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Socket:
            timeout_s: float | None = None

            def gettimeout(self) -> float | None:
                return self.timeout_s

            def settimeout(self, timeout_s: float | None) -> None:
                self.timeout_s = timeout_s

        class Client:
            sent: list[bytes] = []
            lines = [b"+ idling\r\n", b"* 1 EXISTS\r\n", b"OMOIDLE OK IDLE terminated\r\n"]
            sock = Socket()

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def socket(self) -> Socket:
                return self.sock

            def readline(self) -> bytes:
                return self.lines.pop(0)

        client = Client()
        with patch.object(watcher.select, "select", return_value=([object()], [], [])):
            watcher.idle_once(client, 0)
        self.assertEqual([], client.lines)
        self.assertEqual([b"OMOIDLE IDLE\r\n", b"DONE\r\n"], client.sent)

    def test_email_watcher_manager_file_is_configurable(self) -> None:
        from omo_manager.email_idle_watcher import parse_args

        args = parse_args(["--root", "/tmp/root", "--manager-file", "work_manager_20260527.md", "--once"])
        self.assertEqual(Path("/tmp/root/work_manager_20260527.md"), args.manager_file)

    def test_email_watcher_ignores_legacy_active_log_env_by_default(self) -> None:
        from unittest.mock import patch
        from omo_manager.email_idle_watcher import parse_args

        with patch.dict(os.environ, {"OMO_MANAGER_ACTIVE_LOG": "/tmp/root/work_manager.md"}):
            args = parse_args(["--root", "/tmp/root", "--once"])
        self.assertIsNone(args.manager_file)
        self.assertEqual(dated_manager_file(Path("/tmp/root")), current_manager_file(args))

    def test_write_mail_omits_redundant_self_headers(self) -> None:
        from email.message import EmailMessage
        from omo_manager.email_idle_watcher import Args as EmailArgs, write_mail

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            msg = EmailMessage()
            msg["From"] = "Steven Sīchàng Hé <stevensichanghe@gmail.com>"
            msg["Subject"] = "Re: [omo_manager] Update"
            msg["Date"] = "Mon, 25 May 2026 15:09:06 -0700"
            msg.set_content("body\n")
            args = EmailArgs(root, "http://127.0.0.1:18790", root / "manager_mail", root / "state", root / "work_manager.md", True, "self@example.test", 900, Path("/bin/false"))
            path = write_mail(args, "4146", msg, str(msg["From"]), str(msg["Subject"]))
            text = path.read_text(encoding="utf-8")
            self.assertIn("Subject: Re: Update\n", text)
            self.assertNotIn("From:", text)
            self.assertNotIn("Date:", text)
            self.assertNotIn("UID:", text)

    def test_email_watcher_normalizes_manager_reply_subjects_for_storage(self) -> None:
        self.assertEqual("Re: Book demo regression fixed pb_book_demo_regression_5818.md", normalize_human_subject("Re: [omo_manager] Book demo regression fixed pb_book_demo_regression_5818.md"))
        self.assertEqual("Re: Book demo regression fixed pb_book_demo_regression_5818.md", normalize_human_subject("Re: [a] Book demo regression fixed pb_book_demo_regression_5818.md"))
        self.assertEqual("Re: VL supervisor follow-up vl_supervisor_5410.md", normalize_human_subject("Re:[omo_manager] Re: VL supervisor follow-up vl_supervisor_5410.md"))
        self.assertEqual("Re: PB urgent", normalize_human_subject("Re: wl:9 wl:6 wl:7 PB urgent"))
        self.assertEqual("Re: PB urgent", normalize_human_subject("Re: [a] wl:9 pb:1 vl:2 PB urgent"))
        self.assertEqual("Re: PB urgent", normalize_human_subject("Re: [a] [wl:9] [pb:1] [vl:2] PB urgent"))

    def test_email_watcher_accepts_no_space_manager_reply_subject(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        self.assertIn("Re:[a]", watcher.NORMAL_REPLY_SEARCH_PREFIXES)
        self.assertIn("Re:[omo_manager]", watcher.NORMAL_REPLY_SEARCH_PREFIXES)
        self.assertNotIn("Re: [omo]", watcher.NORMAL_REPLY_SEARCH_PREFIXES)
        self.assertTrue(watcher.is_manager_subject("Re:[a] VL supervisor follow-up vl_supervisor_5410.md"))
        self.assertTrue(watcher.is_manager_subject("Re:[omo_manager] VL supervisor follow-up vl_supervisor_5410.md"))
        self.assertFalse(watcher.is_manager_subject("Re: [omo] direct agent follow-up"))
        self.assertFalse(watcher.is_manager_subject("Re: pb news"))
        self.assertFalse(watcher.is_manager_subject("Re: pb news setup"))
        self.assertNotIn("Re: pb news", watcher.NORMAL_REPLY_SEARCH_PREFIXES)

    def test_email_subject_normalization_strips_re_and_manager_tags(self) -> None:
        self.assertEqual("topic", normalized_subject_key("Re: Re: [a] Topic"))
        self.assertEqual("topic", normalized_subject_key("Re:[omo_manager] Topic"))
        self.assertEqual("topic", normalized_subject_key("[omo] Re: topic"))
        self.assertEqual("topic", normalized_subject_key("Re: wl:9 wl:6 Topic"))
        self.assertEqual("topic", normalized_subject_key("Re: [a] wl:9 pb:1 vl:2 Topic"))
        self.assertEqual("topic", normalized_subject_key("Re: [a] [wl:9] [pb:1] [vl:2] Topic"))
        self.assertEqual("Topic", strip_leading_tmux_tags("wl:9 wl:6 Topic"))
        self.assertEqual("Topic", strip_leading_tmux_tags("[wl:9] [wl:6] Topic"))

    def test_email_subject_recent_thread_uses_reply_subject(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_exists = subject.recent_thread_exists
        subject.recent_thread_exists = lambda key: key == "topic"
        try:
            self.assertEqual("Re: [a] Topic", prepare_subject("Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("[omo_manager] Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("Re: [omo_manager] Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("[a] Re: Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("[a] [omo] Topic"))
        finally:
            subject.recent_thread_exists = old_recent_thread_exists

    def test_email_subject_recent_thread_builds_reply_headers(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_header = subject.recent_thread_header
        subject.recent_thread_header = lambda key: RecentHeader("me@example.test", "Topic", datetime.now().astimezone(), "<prior@example.test>", "<root@example.test>")
        try:
            self.assertEqual(
                {"In-Reply-To": "<prior@example.test>", "References": "<root@example.test> <prior@example.test>"},
                reply_headers_for_subject("Re: [a] Topic"),
            )
        finally:
            subject.recent_thread_header = old_recent_thread_header

    def test_email_subject_prepare_subject_and_headers_uses_one_recent_lookup(self) -> None:
        from omo_manager import omo_email_subject as subject

        calls: list[str] = []
        old_recent_thread_header = subject.recent_thread_header

        def recent_thread_header(key: str) -> RecentHeader:
            calls.append(key)
            return RecentHeader("me@example.test", "Topic", datetime.now().astimezone(), "<prior@example.test>", "<root@example.test>")

        subject.recent_thread_header = recent_thread_header
        try:
            self.assertEqual(
                ("Re: [a] Topic", {"In-Reply-To": "<prior@example.test>", "References": "<root@example.test> <prior@example.test>"}),
                prepare_subject_and_headers("Topic"),
            )
            self.assertEqual(["topic"], calls)
        finally:
            subject.recent_thread_header = old_recent_thread_header

    def test_email_subject_prepends_tmux_target_after_manager_tag(self) -> None:
        self.assertEqual("[a] [wl:7] Topic", manager_subject_w_target("Topic", "wl:7"))
        self.assertEqual("Re: [a] [wl:7] Topic", manager_subject_w_target("Topic", "wl:7", True))
        self.assertEqual("Re: [a] [wl:7] Topic", prepare_subject("Re: [a] Topic", "wl:7"))
        self.assertEqual("Re: [a] [wl:7] Topic", prepare_subject("Re: wl:9 wl:6 Topic", "wl:7"))
        self.assertEqual("Re: [a] [wl:7] Topic", prepare_subject("Re: [a] wl:9 pb:1 vl:2 Topic", "wl:7"))
        self.assertEqual("Re: [a] [wl:7] Topic", prepare_subject("Re: [a] [wl:9] [pb:1] [vl:2] Topic", "wl:7"))
        self.assertEqual("Re: [a] [vl:15] Topic", prepare_subject("Re: [wl:9] [pb:1] [vl:2] Topic", "vl:15"))
        self.assertEqual("[a] [wl:7] Topic", manager_subject_w_target("Topic", "wl:7.0"))
        self.assertEqual("Re: [a] [wl:7] Topic", prepare_subject("Re: [a] Topic", "wl:7.0"))
        self.assertEqual("[a] [wl:7.1] Topic", manager_subject_w_target("Topic", "wl:7.1"))
        self.assertEqual("[a] Topic", prepare_subject("Topic", "not-a-target"))

    def test_email_subject_target_keeps_recent_thread_lookup_key_untargeted(self) -> None:
        from omo_manager import omo_email_subject as subject

        calls: list[str] = []
        old_recent_thread_header = subject.recent_thread_header

        def recent_thread_header(key: str) -> None:
            calls.append(key)
            return None

        subject.recent_thread_header = recent_thread_header
        try:
            self.assertEqual(("[a] [wl:7] Topic", {}), prepare_subject_and_headers("Topic", "wl:7"))
            self.assertEqual(["topic"], calls)
        finally:
            subject.recent_thread_header = old_recent_thread_header

    def test_email_subject_fetch_recent_header_reads_thread_headers(self) -> None:
        header_date = format_datetime(datetime.now().astimezone())

        class FakeClient:
            def uid(self, _command: str, *_args: str) -> tuple[str, list[tuple[bytes, bytes]]]:
                return (
                    "OK",
                    [
                        (
                            b"1",
                            (f"Date: {header_date}\r\nFrom: me@example.test\r\nSubject: Topic\r\nMessage-ID: <prior@example.test>\r\nReferences: <root@example.test>\r\n\r\n").encode(),
                        )
                    ],
                )

        header = fetch_recent_header(FakeClient(), "1")  # type: ignore[arg-type]
        self.assertIsNotNone(header)
        assert header is not None
        self.assertEqual("<prior@example.test>", header.message_id)
        self.assertEqual("<root@example.test>", header.references)

    def test_email_subject_lookup_error_falls_back_to_new_tag(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_exists = subject.recent_thread_exists
        subject.recent_thread_exists = lambda _key: (_ for _ in ()).throw(RuntimeError("imap down"))
        try:
            self.assertEqual("[a] Topic", prepare_subject("Topic"))
            self.assertEqual("[a] Topic", prepare_subject("[omo_manager] Topic"))
        finally:
            subject.recent_thread_exists = old_recent_thread_exists

    def test_email_subject_lookup_deadline_falls_back_for_recorded_timeout_subject(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_exists = subject.recent_thread_exists
        subject.recent_thread_exists = lambda _key: time.sleep(10) or False
        try:
            started_s = time.monotonic()
            with patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S": "0.05"}):
                self.assertEqual(
                    "[a] Updates on manager email filtering manager_market_alert_email_filter_7564.md", prepare_subject("Updates on manager email filtering manager_market_alert_email_filter_7564.md")
                )
            self.assertLess(time.monotonic() - started_s, 1.0)
        finally:
            subject.recent_thread_exists = old_recent_thread_exists

    def test_email_subject_lookup_deadline_skips_slow_logout(self) -> None:
        from omo_manager import omo_email_subject as subject

        self.assertFalse(issubclass(subject.SubjectLookupTimeout, OSError))

        class SlowLogoutClient:
            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                self.readonly = readonly
                return ("OK", [])

            def uid(self, _command: str, *_args: str) -> tuple[str, list[bytes]]:
                time.sleep(10)
                return ("OK", [])

            def logout(self) -> None:
                time.sleep(10)

            def shutdown(self) -> None:
                return None

        started_s = time.monotonic()
        with (
            patch.object(subject.imaplib, "IMAP4_SSL", return_value=SlowLogoutClient()),
            patch.object(subject, "parse_env_config", return_value={"host": "imap.example", "user": "me@example.com", "password": "secret"}),
            patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S": "0.05"}),
        ):
            self.assertFalse(subject.has_recent_thread("topic"))
        self.assertLess(time.monotonic() - started_s, 1.0)

    def test_email_subject_slow_logout_preserves_found_recent_thread(self) -> None:
        from omo_manager import omo_email_subject as subject

        header_date = format_datetime(datetime.now().astimezone())

        class MatchingSlowLogoutClient:
            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                self.readonly = readonly
                return ("OK", [])

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes] | list[tuple[bytes, bytes]]]:
                if command == "search":
                    return ("OK", [b"1"])
                return ("OK", [(b"1", f"Date: {header_date}\r\nFrom: me@example.com\r\nSubject: Topic\r\n\r\n".encode())])

            def logout(self) -> None:
                time.sleep(10)

            def shutdown(self) -> None:
                return None

        started_s = time.monotonic()
        with (
            patch.object(subject.imaplib, "IMAP4_SSL", return_value=MatchingSlowLogoutClient()),
            patch.object(subject, "parse_env_config", return_value={"host": "imap.example", "user": "me@example.com", "password": "secret"}),
            patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S": "0.05"}),
        ):
            self.assertTrue(subject.has_recent_thread("topic"))
        self.assertLess(time.monotonic() - started_s, 1.0)

    def test_legacy_email_source_block_is_delivered_by_pending_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dated_manager_file(root)
            _ = path.write_text("(pending)\n[source: email manager_mail/3979.txt]\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))

    def test_email_watcher_submits_new_pending_ref_before_mark_seen(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] submit me"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"12"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file.relative_to(args.root), line_no, args.manager_target))
                return True

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(Path("work_manager_today.md"), 2, "wl:1.0")])
            self.assertEqual(len(client.stores), 1)
            self.assertIn("12	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_direct_push_runs_submit_before_mark_read(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            manager_file = root / "work_manager_today.md"
            state = Path(tmp) / "state"
            calls = []

            def run(push: watcher.EmailPush) -> bool:
                calls.append(push)
                return True

            old_run = watcher.run_email_push
            watcher.run_email_push = run
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.push_email_ref(args, 2))
                self.assertTrue(watcher.push_email_ref(args, 3))
            finally:
                watcher.run_email_push = old_run
            self.assertEqual([2, 3], [push.line_no for push in calls])
            push = calls[0]
            self.assertEqual("wl:1.0", push.target)
            self.assertEqual(root, push.root)
            self.assertEqual(Path("work_manager_today.md"), push.pending_file)
            self.assertEqual("pending: file=work_manager_today.md line=2 origin=human source=email action=ack-human", push.text)

            def fail(_push: watcher.EmailPush) -> bool:
                return False

            watcher.run_email_push = fail
            try:
                self.assertFalse(watcher.push_email_ref(args, 4))
            finally:
                watcher.run_email_push = old_run

    def test_email_watcher_tracks_manager_mail_counts(self) -> None:
        from datetime import datetime
        from email.message import EmailMessage
        from email.utils import format_datetime
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            recent_date = format_datetime(datetime.now().astimezone())

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b"2 3"]
                        if "SINCE" in args:
                            return "OK", [b"1 2 3"]
                        return "OK", [b"1 2 3 4"]
                    if command == "fetch":
                        msg = EmailMessage()
                        msg["From"] = "Manager <me@example.com>"
                        msg["To"] = "Manager <me@example.com>"
                        msg["Subject"] = "[omo_manager] item"
                        msg["Date"] = recent_date
                        msg.set_content("body")
                        return "OK", [(b"HEADER", msg.as_bytes())]
                    raise AssertionError(command)

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = lambda *_args: (_ for _ in ()).throw(AssertionError("threshold should not trigger"))
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            counts_text = (state / "email-manager-mail-counts.tsv").read_text(encoding="utf-8")
            self.assertIn("manager_total\t4\n", counts_text)
            self.assertIn("manager_unread\t2\n", counts_text)
            self.assertIn("manager_human_recent_total\t3\n", counts_text)

    def test_email_watcher_threshold_counts_ignore_ignored_uids(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-ignored-uids.tsv").write_text("1\t1\n2\t1\n3\t1\n", encoding="utf-8")

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b"1 2 3"]
                        if "SINCE" in args:
                            return "OK", [b"1 2 3"]
                        return "OK", [b"1 2 3"]
                    if command == "fetch":
                        msg = EmailMessage()
                        msg["From"] = "Manager <me@example.com>"
                        msg["To"] = "Manager <me@example.com>"
                        msg["Subject"] = "[omo_manager] item"
                        msg.set_content("body")
                        return "OK", [(b"HEADER", msg.as_bytes())]
                    raise AssertionError(command)

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = lambda *_args: (_ for _ in ()).throw(AssertionError("threshold should not trigger"))
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            counts_text = (state / "email-manager-mail-counts.tsv").read_text(encoding="utf-8")
            self.assertIn("manager_total\t0\n", counts_text)
            self.assertIn("manager_unread\t0\n", counts_text)
            self.assertIn("manager_human_recent_total\t0\n", counts_text)

    def test_email_watcher_triggers_unread_compression_once(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            total = " ".join(str(uid) for uid in range(1, 21)).encode()
            unread = " ".join(str(uid) for uid in range(1, 18)).encode()
            calls: list[tuple[int, str]] = []

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN":
                            return "OK", [unread]
                        return "OK", [total]
                    if command == "fetch":
                        msg = EmailMessage()
                        msg["From"] = "Manager <me@example.com>"
                        msg["To"] = "Manager <me@example.com>"
                        msg["Subject"] = "[omo_manager] item"
                        msg.set_content("body")
                        return "OK", [(b"HEADER", msg.as_bytes())]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int, kind: str) -> bool:
                calls.append((line_no, kind))
                return True

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = push
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.handle_manager_mail_thresholds(Client(), args))
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            manager_text = (root / "work_manager_today.md").read_text(encoding="utf-8")
            self.assertEqual([(2, "unread-compression")], calls)
            self.assertEqual(1, manager_text.count(watcher.threshold_marker("unread-compression")))
            self.assertIn("docs/mail/compression.md", manager_text)
            self.assertIn("unread-compression\t1\n", (state / "email-manager-mail-thresholds.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_unread_threshold_can_retrigger_after_count_drops(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            calls: list[tuple[int, str]] = []

            class Client:
                unread_n = 17

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN":
                            return "OK", [" ".join(str(uid) for uid in range(1, self.unread_n + 1)).encode()]
                        return "OK", [b"1 2 3 4"]
                    if command == "fetch":
                        msg = EmailMessage()
                        msg["From"] = "Manager <me@example.com>"
                        msg["To"] = "Manager <me@example.com>"
                        msg["Subject"] = "[omo_manager] item"
                        msg.set_content("body")
                        return "OK", [(b"HEADER", msg.as_bytes())]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int, kind: str) -> bool:
                calls.append((line_no, kind))
                return True

            client = Client()
            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = push
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.handle_manager_mail_thresholds(client, args))
                client.unread_n = 16
                self.assertFalse(watcher.handle_manager_mail_thresholds(client, args))
                client.unread_n = 17
                self.assertTrue(watcher.handle_manager_mail_thresholds(client, args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            manager_text = (root / "work_manager_today.md").read_text(encoding="utf-8")
            self.assertEqual([(2, "unread-compression"), (10, "unread-compression")], calls)
            self.assertEqual(2, manager_text.count(watcher.threshold_marker("unread-compression")))

    def test_email_watcher_recent_cleanup_threshold_filters_to_last_24h(self) -> None:
        from datetime import datetime, timedelta
        from email.message import EmailMessage
        from email.utils import format_datetime
        from omo_manager import email_idle_watcher as watcher

        now = datetime.now().astimezone()
        recent_date = format_datetime(now - timedelta(hours=1))
        old_date = format_datetime(now - timedelta(hours=25))
        uids = [str(uid) for uid in range(1, 66)]

        class Client:
            def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                if command == "search":
                    if "SINCE" in args:
                        return "OK", [" ".join(uids).encode()]
                    return "OK", [b""]
                if command == "fetch":
                    uid = str(args[0])
                    msg = EmailMessage()
                    msg["From"] = "Manager <me@example.com>"
                    msg["To"] = "Manager <me@example.com>"
                    msg["Subject"] = "[omo_manager] item"
                    msg["Date"] = old_date if uid == "65" else recent_date
                    msg.set_content("body")
                    return "OK", [(b"HEADER", msg.as_bytes())]
                raise AssertionError(command)

        counts = watcher.manager_mail_counts(Client(), "me@example.com", 24 * 60 * 60, 64, now)
        self.assertTrue(counts.recent_exact)
        self.assertEqual(64, counts.recent_total)

    def test_email_watcher_threshold_counts_require_self_recipient(self) -> None:
        from datetime import datetime
        from omo_manager import email_idle_watcher as watcher

        now = datetime.now().astimezone()

        class Client:
            def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                if command == "search":
                    if "TO" not in args:
                        raise AssertionError("manager mail search must include recipient boundary")
                    return "OK", [b""]
                if command == "fetch":
                    raise AssertionError("non-self-addressed search results should be excluded before fetch")
                raise AssertionError(command)

        counts = watcher.manager_mail_counts(Client(), "me@example.com", 24 * 60 * 60, 64, now)
        self.assertEqual(0, counts.total)
        self.assertEqual(0, counts.unread)
        self.assertEqual(0, counts.recent_total)

    def test_email_watcher_triggers_recent_cleanup_once(self) -> None:
        from datetime import datetime, timedelta
        from email.message import EmailMessage
        from email.utils import format_datetime
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            uids = [str(uid) for uid in range(1, 66)]
            encoded_uids = " ".join(uids).encode()
            recent_date = format_datetime(datetime.now().astimezone() - timedelta(hours=1))
            calls: list[tuple[int, str]] = []

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b""]
                        return "OK", [encoded_uids]
                    if command == "fetch":
                        msg = EmailMessage()
                        msg["From"] = "Manager <me@example.com>"
                        msg["To"] = "Manager <me@example.com>"
                        msg["Subject"] = "[omo_manager] item"
                        msg["Date"] = recent_date
                        msg.set_content("body")
                        return "OK", [(b"HEADER", msg.as_bytes())]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int, kind: str) -> bool:
                calls.append((line_no, kind))
                return True

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = push
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.handle_manager_mail_thresholds(Client(), args))
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            manager_text = (root / "work_manager_today.md").read_text(encoding="utf-8")
            self.assertEqual([(2, "recent-cleanup")], calls)
            self.assertEqual(1, manager_text.count(watcher.threshold_marker("recent-cleanup")))
            self.assertIn("docs/mail/cleanup.md", manager_text)
            self.assertIn("recent-cleanup\t1\n", (state / "email-manager-mail-thresholds.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_async_worker_start_failure_can_retry(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Thread:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread start failed")

        old_thread = watcher.threading.Thread
        old_started = watcher._email_push_worker_started
        watcher.threading.Thread = Thread
        watcher._email_push_worker_started = False
        try:
            with self.assertRaises(RuntimeError):
                watcher.start_email_push_worker()
            self.assertFalse(watcher._email_push_worker_started)
        finally:
            watcher.threading.Thread = old_thread
            watcher._email_push_worker_started = old_started

    def test_email_watcher_keeps_existing_pending_unread_when_submit_fails(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            mail = root / "manager_mail" / "13.txt"
            mail.parent.mkdir()
            mail.write_text("body", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(pending)\n(from email manager_mail/13.txt)\n", encoding="utf-8")
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] retry me"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN" and self.stores:
                            return "OK", [b""]
                        if "UID" in args and "14:*" in args:
                            return "OK", [b""]
                        return "OK", [b"13"]
                    if command == "fetch":
                        raise AssertionError("existing pending should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return False

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [1, 1])
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_marks_existing_pending_read_after_submit_accepts(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            mail = root / "manager_mail" / "13.txt"
            mail.parent.mkdir()
            mail.write_text("body", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(pending)\n(from email manager_mail/13.txt)\n", encoding="utf-8")
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] retry me"
            msg.set_content("PWD: /tmp/agent-work\n\nagent body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN" and self.stores:
                            return "OK", [b""]
                        if "UID" in args and "14:*" in args:
                            return "OK", [b""]
                        return "OK", [b"13"]
                    if command == "fetch":
                        raise AssertionError("existing pending should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return True

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [1])
            self.assertEqual(client.stores, [("13", "+FLAGS", r"(\Seen)")])
            self.assertIn("13	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_rejects_omo_agent_reply_with_pwd(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo] direct agent reply"
            msg.set_content("body\n\nPWD: /tmp/agent-work\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"48"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0", recent_cleanup_threshold=999)
            watcher.handle_unseen(client, args)
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail" / "48.txt").exists())
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_rejects_pb_news_reply(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: pb news"
            msg.set_content("PB news reply body.\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"50"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail" / "50.txt").exists())
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_ignores_manager_authored_reply_echo_with_pwd(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Manager <me@example.com>"
            msg["Subject"] = "Re: [a] Update on manager_email_watcher_read_after_forward_6901.md"
            msg.set_content(
                "Acknowledged. I will route this to a separate VL worker now.\n\n"
                "Scope I am giving it: inspect why VL build artifacts leaked into the manager work-log PWD.\n\n"
                "PWD: /ssd1/sichangheagent/work_logs\n"
            )

            class Client:
                fetches = 0
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"49"]
                    if command == "fetch":
                        self.fetches += 1
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            watcher.handle_unseen(client, args)
            self.assertEqual(1, client.fetches)
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail" / "49.txt").exists())
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())
            self.assertIn("49\t", (state / "email-ignored-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_ignores_manager_authored_reply_echo_with_tmux_footer(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        for uid, footer in (("51", "tmux: wl:7\n"), ("52", "tmux: pb-watch-loop:0\n")):
            with self.subTest(footer=footer):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "logs"
                    root.mkdir()
                    state = Path(tmp) / "state"
                    msg = EmailMessage()
                    msg["From"] = "Manager <me@example.com>"
                    msg["Subject"] = "Re: [a] Update on manager_email_watcher_read_after_forward_6901.md"
                    msg.set_content(f"Acknowledged. I will route this to a separate worker now.\n\n{footer}")

                    class Client:
                        fetches = 0
                        stores: list[tuple[object, ...]] = []

                        def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                            if command == "search":
                                if "SINCE" in args:
                                    return "OK", [b""]
                                return "OK", [uid.encode()]
                            if command == "fetch":
                                self.fetches += 1
                                return "OK", [(b"RFC822", msg.as_bytes())]
                            if command == "store":
                                self.stores.append(args)
                                return "OK", [b""]
                            raise AssertionError(command)

                    client = Client()
                    manager_file = root / "work_manager_today.md"
                    args = watcher.Args(
                        root,
                        "",
                        root / "manager_mail",
                        state,
                        manager_file,
                        True,
                        "me@example.com",
                        0,
                        Path("/bin/false"),
                        manager_target="wl:1.0",
                    )
                    watcher.handle_unseen(client, args)
                    watcher.handle_unseen(client, args)
                    self.assertEqual(1, client.fetches)
                    self.assertFalse(manager_file.exists())
                    self.assertFalse((root / "manager_mail" / f"{uid}.txt").exists())
                    self.assertEqual(client.stores, [])
                    self.assertFalse((state / "email-processed-uids.tsv").exists())
                    self.assertIn(f"{uid}\t", (state / "email-ignored-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_classifies_plain_manager_subject_with_footer(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        msg = EmailMessage()
        msg["From"] = "Manager <me@example.com>"
        msg["Subject"] = "[a] manager status"
        msg.set_content("Status.\n\ntmux: wl:16\n")

        self.assertTrue(watcher.manager_authored_message(msg, "me@example.com"))

    def test_email_watcher_does_not_mark_manager_authored_existing_source_read(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            mail = root / "manager_mail" / "8910.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: Re: [a] status\n\nManager status.\n\ntmux: wl:16\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done: already summarized manager_mail/8910.txt)\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Manager <me@example.com>"
            msg["Subject"] = "[a] manager status"
            msg.set_content("Manager status.\n\ntmux: wl:16\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"8910"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual(client.stores, [])
            self.assertIn("8910\t", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_marks_human_existing_source_with_footer_like_body_read(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            mail = root / "manager_mail" / "8911.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: Re: [a] human reply\n\nPlease inspect this.\n\ntmux: wl:16\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done: already handled manager_mail/8911.txt)\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] human reply"
            msg.set_content("Please inspect this.\n\ntmux: wl:16\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"8911"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual(client.stores, [("8911", "+FLAGS", r"(\Seen)")])
            self.assertIn("8911\t", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_queues_fresh_human_reply_with_footer_like_body(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] human reply"
            msg.set_content("Please inspect this.\n\ntmux: wl:16\n")
            calls: list[int] = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"8912"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return True

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                manager_file = root / "work_manager_today.md"
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [2])
            self.assertIn("(pending)\n(record and delegate manager_mail/8912.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("8912", "+FLAGS", r"(\Seen)")])
            self.assertNotIn("8912\t", (state / "email-ignored-uids.tsv").read_text(encoding="utf-8") if (state / "email-ignored-uids.tsv").exists() else "")

    def test_email_watcher_does_not_ignore_quoted_tmux_footer(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] Update on manager_email_watcher_read_after_forward_6901.md"
            msg.set_content("Human reply quoting prior footer.\n\n> tmux: wl:7\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"53"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            calls = []
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "me@example.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1.0",
            )
            old_push = watcher.push_email_ref

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return False

            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(record and delegate manager_mail/53.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(calls, [2])
            self.assertFalse((state / "email-ignored-uids.tsv").exists())

    def test_email_watcher_agent_footer_recognition_is_parseable(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        self.assertTrue(watcher.has_agent_footer("body\n\ntmux: wl:2\n"))
        self.assertTrue(watcher.has_agent_footer("body\n\ntmux: wl:7\n"))
        self.assertTrue(watcher.has_agent_footer("body\n\ntmux: pb-watch-loop:0\n"))
        self.assertTrue(watcher.has_agent_footer("body\r\n\r\ntmux: wl:2\r\n"))
        self.assertTrue(watcher.has_agent_footer("body\n\nTMUX: wl:2\n"))
        self.assertTrue(watcher.has_agent_footer("body\n\ntmux: notes\n"))
        self.assertFalse(watcher.has_agent_footer("body\n\n> tmux: wl:2\n"))

    def test_email_watcher_recovers_processed_uid_without_current_source(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("41\t1\n", encoding="utf-8")
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] stale root"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b""]
                        return "OK", [b"41"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return False

            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(record and delegate manager_mail/41.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(calls, [2, 2])
            self.assertEqual(client.stores, [])
            self.assertIn("41	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_processed_uid_with_old_root_source_can_mark_read(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("42\t1\n", encoding="utf-8")
            (root / "work_manager_2026-06-13.md").write_text("(pending)\n(from email manager_mail/42.txt)\n", encoding="utf-8")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"42"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_2026-06-14.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual(client.stores, [("42", "+FLAGS", r"(\Seen)")])

    def test_email_watcher_retries_unaccepted_processed_pending_in_old_log(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("43\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("43\t1\n", encoding="utf-8")
            old_log = root / "work_manager_2026-06-13.md"
            old_log.write_text("(pending)\n(from email manager_mail/43.txt)\n", encoding="utf-8")
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"43"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return False

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_2026-06-14.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(old_log, 1)])
            self.assertEqual(client.stores, [])
            self.assertIn("43	", (state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_clears_unaccepted_pending_after_submit_accepts(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("49\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("49\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            manager_file.write_text("(pending)\n(from email manager_mail/49.txt)\n", encoding="utf-8")
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if self.stores:
                            return "OK", [b""]
                        return "OK", [b"49"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return True

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(manager_file, 1)])
            self.assertEqual(client.stores, [("49", "+FLAGS", r"(\Seen)")])
            self.assertFalse((state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8").strip())

    def test_email_watcher_retries_unaccepted_unprocessed_pending_in_old_log(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-unaccepted-pending-uids.tsv").write_text("44\t1\n", encoding="utf-8")
            old_log = root / "work_manager_2026-06-13.md"
            old_log.write_text("(pending)\n(from email manager_mail/44.txt)\n", encoding="utf-8")
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"44"]
                    if command == "fetch":
                        raise AssertionError("unaccepted old pending should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return False

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_2026-06-14.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(old_log, 1)])
            self.assertEqual(client.stores, [])
            self.assertFalse((root / "work_manager_2026-06-14.md").exists())

    def test_email_watcher_accepts_processed_unaccepted_source_without_pending(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("45\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("45\t1\n", encoding="utf-8")
            old_log = root / "work_manager_2026-06-13.md"
            old_log.write_text("(done)\n(from email manager_mail/45.txt)\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] source only"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"45"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertFalse(manager_file.exists())
            self.assertEqual(client.stores, [("45", "+FLAGS", r"(\Seen)")])
            self.assertFalse((state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8").strip())

    def test_email_watcher_accepts_processed_unaccepted_consumed_status_line(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("8774\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("8774\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text(
                "(done: duplicate `manager_mail/8774.txt` marker consumed; already delivered.)\n",
                encoding="utf-8",
            )
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] consumed status"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if self.stores:
                            return "OK", [b""]
                        if "UID" in args:
                            uid_arg = str(args[args.index("UID") + 1])
                            return ("OK", [b"8774"]) if "8774" in uid_arg else ("OK", [b""])
                        return "OK", [b"8774"]
                    if command == "fetch":
                        raise AssertionError("accepted consumed status should not refetch mail")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            try:
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(client.stores, [("8774", "+FLAGS", r"(\Seen)")])
            self.assertFalse((state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8").strip())

    def test_email_watcher_reprocesses_old_processed_unaccepted_uid_without_source(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("45\t1\n500\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("45\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] old unaccepted"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if "UID" in args and "45" in args:
                            return "OK", [b"45"]
                        return "OK", [b""]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(record and delegate manager_mail/45.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [])
            self.assertIn("45\t", (state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_accepts_unprocessed_stale_unaccepted_source(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-unaccepted-pending-uids.tsv").write_text("46\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            manager_file.write_text("(done)\n(from email manager_mail/46.txt)\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] source only"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"46"]
                    if command == "fetch":
                        raise AssertionError("consumed source should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual("(done)\n(from email manager_mail/46.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("46", "+FLAGS", r"(\Seen)")])
            self.assertIn("46\t", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))
            self.assertFalse((state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8").strip())

    def test_email_watcher_retries_unaccepted_pending_with_distant_source_marker(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("47\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("47\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            manager_file.write_text(
                "(pending)\nmanager note line 1\nmanager note line 2\nmanager note line 3\nmanager note line 4\n(from email manager_mail/47.txt)\n",
                encoding="utf-8",
            )
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"47"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return False

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(manager_file, 1)])
            self.assertEqual(client.stores, [])

    def test_email_watcher_keeps_new_pending_unread_when_submit_fails(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] durable first"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"14"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                client = Client()
                manager_file = root / "work_manager_today.md"
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(record and delegate manager_mail/14.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_source_without_pending_stays_unread_when_submit_fails(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done)\n(from email manager_mail/17.txt)\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] source only"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"17"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            text = manager_file.read_text(encoding="utf-8")
            self.assertIn("(done)\n(from email manager_mail/17.txt)\n\n(pending)\n(record and delegate manager_mail/17.txt)\n", text)
            self.assertEqual(client.stores, [])

    def test_email_watcher_processed_source_without_pending_marks_read_without_duplicate_pending(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("18\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done)\n(from email manager_mail/18.txt)\n", encoding="utf-8")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"18"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual("(done)\n(from email manager_mail/18.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("18", "+FLAGS", r"(\Seen)")])

    def test_email_watcher_processed_consumed_source_marks_read_without_duplicate_pending(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("8867\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(manager routed: manager_mail/8867.txt to wl:16)\n", encoding="utf-8")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"8867"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    if command == "fetch":
                        raise AssertionError("processed consumed source should be accepted before fetching")
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            with patch("omo_manager.email_idle_watcher.maybe_handle_manager_mail_thresholds", return_value=False):
                watcher.handle_unseen(client, args)
            self.assertEqual("(manager routed: manager_mail/8867.txt to wl:16)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("8867", "+FLAGS", r"(\Seen)")])

    def test_email_watcher_unprocessed_routed_source_marks_read_without_duplicate_pending(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(manager routed: manager_mail/8867.txt to wl:16)\n", encoding="utf-8")
            pushes: list[int] = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"8867"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    if command == "fetch":
                        raise AssertionError("already-routed source should be accepted before fetching")
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda _args, line_no: pushes.append(line_no) is None
            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0", recent_cleanup_threshold=999)
            try:
                with patch("omo_manager.email_idle_watcher.maybe_handle_manager_mail_thresholds", return_value=False):
                    watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual("(manager routed: manager_mail/8867.txt to wl:16)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual([], pushes)
            self.assertEqual(client.stores, [("8867", "+FLAGS", r"(\Seen)")])
            self.assertIn("8867\t", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_legacy_routed_prose_source_marks_read_without_duplicate_pending(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            manager_file = root / "work_manager_today.md"
            manager_file.write_text(
                "(pending)\n"
                "(manager routed: to `task.md`.)\n"
                "(from email manager_mail/4480.txt)\n",
                encoding="utf-8",
            )
            pushes: list[int] = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"4480"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    if command == "fetch":
                        raise AssertionError("routed prose source should be accepted before fetching")
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda _args, line_no: pushes.append(line_no) is None
            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            try:
                with patch("omo_manager.email_idle_watcher.maybe_handle_manager_mail_thresholds", return_value=False):
                    watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(1, manager_file.read_text(encoding="utf-8").count("manager_mail/4480.txt"))
            self.assertEqual([], pushes)
            self.assertEqual(client.stores, [("4480", "+FLAGS", r"(\Seen)")])
            self.assertIn("4480\t", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_does_not_recover_old_processed_uid_without_source(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("18\t1\n500\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done)\n", encoding="utf-8")

            class Client:
                fetches = 0

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        joined = " ".join(str(arg) for arg in args)
                        if "UID" in args and "18" in args:
                            raise AssertionError("old UID should not be searched for recovery")
                        if "UNSEEN" in joined:
                            return "OK", [b"18"]
                        return "OK", [b""]
                    if command == "fetch":
                        self.fetches += 1
                        raise AssertionError("old processed UID outside recovery window should not be fetched")
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual("(done)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(0, client.fetches)

    def test_email_watcher_keeps_processed_state_when_mark_seen_returns_no(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] mark read fails"
            msg.set_content("body")

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"15"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        return "NO", [b"temporary failure"]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: True
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(Client(), args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(record and delegate manager_mail/15.txt)\n", (root / "work_manager_today.md").read_text(encoding="utf-8"))
            self.assertIn("15	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_searches_unseen_and_seen_uid_range_after_processed_state(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("12\t1\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] already seen"
            msg.set_content("body")

            class Client:
                searches: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b""]
                        self.searches.append(args)
                        return "OK", [b"13"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: True
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertTrue(client.searches)
            self.assertEqual(client.searches[0][:6], (None, "UID", "13:*", "FROM", '"me@example.com"', "SUBJECT"))
            self.assertIn("13	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_marks_new_pending_read_after_submit_succeeds(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] push raises"
            msg.set_content("body\n\n> PWD: /tmp/agent-work\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"16"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            pushes = []

            def run(push: watcher.EmailPush) -> bool:
                pushes.append(push)
                return True

            old_run = watcher.run_email_push
            watcher.run_email_push = run
            try:
                client = Client()
                manager_file = root / "work_manager_today.md"
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.run_email_push = old_run
            self.assertIn("(pending)\n(record and delegate manager_mail/16.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("16", "+FLAGS", r"(\Seen)")])
            self.assertIn("16	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))
            self.assertEqual(1, len(pushes))
            self.assertEqual(2, pushes[0].line_no)

    def test_email_watcher_processes_lower_unread_uid_after_higher_processed_uid(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("20\t1\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] lower uid"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b"19"]
                        return "OK", [b""]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: True
            try:
                client = Client()
                manager_file = root / "work_manager_today.md"
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(record and delegate manager_mail/19.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("19", "+FLAGS", r"(\Seen)")])
            self.assertIn("19	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_omo_report_writes_agent_pending_source_without_direct_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            duplicate_msg = Path(tmp) / "duplicate-msg.md"
            _ = msg.write_text("done\n", encoding="utf-8")
            _ = duplicate_msg.write_text("done\n", encoding="utf-8")
            task = write_report_worker_task(root)
            for message_file in (msg, duplicate_msg):
                result = subprocess.run(
                    omo_report_command(agent="agent-4002", message_file=message_file),
                    cwd=tmp,
                    env=report_test_env(tmp),
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            manager_log = dated_manager_file(root)
            text = manager_log.read_text(encoding="utf-8")
            self.assertRegex(text, re.compile(r"^\(pending\)\n\(from agent agent:1 /tmp/omo-agent-messages-\d+/agent-4002_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))
            self.assertNotIn("[omo-message-source: ", text)
            self.assertNotIn("(report manager ", text)
            self.assertNotIn("[message-sha256: ", text)
            self.assertNotIn("message-file: ", text)
            self.assertNotIn("message:\n", text)
            self.assertNotIn("> done", text)
            report_paths = agent_pointer_paths(text)
            self.assertEqual(1, len(report_paths))
            report_path = report_paths[0]
            self.assertEqual(Path("/tmp") / f"omo-agent-messages-{os.getuid()}", report_path.parent)
            report_text = report_path.read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="agent-4002", tmux="agent:1", task_file="task.md", message=b"done\n")
            self.assertTrue(report_text.endswith("message:\ndone\n"))
            self.assertEqual(0o600, report_path.stat().st_mode & 0o777)
            self.assertEqual(0o700, report_path.parent.stat().st_mode & 0o777)
            self.assertNotIn("PWD:", text)
            self.assertNotIn("OPENCODE:", text)
            self.assertNotIn("TMUX:", text)
            self.assertNotIn("(pending)", task.read_text(encoding="utf-8"))
            markers = find_markers(root, [manager_log])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)

    def test_omo_report_allocated_message_file_submits_with_default_inference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            write_report_worker_task(root)

            allocated = subprocess.run(
                omo_report_alloc_command(),
                cwd=tmp,
                env=report_test_env(tmp),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", allocated.stderr)
            self.assertEqual(0, allocated.returncode)
            report_file = Path(allocated.stdout.strip())
            self.assertEqual(Path("/tmp") / f"omo-report-drafts-{os.getuid()}", report_file.parent)
            self.assertTrue(report_file.name.startswith("task."))
            self.assertEqual(0o600, report_file.stat().st_mode & 0o777)
            self.assertEqual(0o700, report_file.parent.stat().st_mode & 0o777)
            report_file.write_text("allocated done\n", encoding="utf-8")

            submitted = subprocess.run(
                omo_report_command(agent="agent-4002", message_file=report_file),
                cwd=tmp,
                env=report_test_env(tmp),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", submitted.stderr)
            self.assertEqual(0, submitted.returncode)
            manager_text = dated_manager_file(root).read_text(encoding="utf-8")
            report_text = agent_pointer_paths(manager_text)[0].read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="agent-4002", tmux="agent:1", task_file="task.md", message=b"allocated done\n")

    def test_omo_report_rejects_pruned_explicit_route_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            write_report_worker_task(root)
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")

            for flag, value in (("--root", str(root)), ("--task-file", "task.md"), ("--manager-url", "http://127.0.0.1:1")):
                result = subprocess.run(
                    [omo_report_script(), flag, value, "--status", "done", "--message-file", str(msg)],
                    cwd=tmp,
                    env=report_test_env(tmp),
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

                self.assertEqual(2, result.returncode)
                self.assertIn(f"unknown argument: {flag}", result.stderr)

    def test_omo_report_main_manager_alias_does_not_need_env_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            _ = write_report_worker_task(root, "task.md", managerat="main:0.0")

            result = subprocess.run(
                omo_report_command(agent="agent-4002", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp, manager_target=""),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            manager_text = dated_manager_file(root).read_text(encoding="utf-8")
            self.assertIn("(pending)", manager_text)
            self.assertNotIn("(pending)", (root / "task.md").read_text(encoding="utf-8"))

    def test_omo_report_main_manager_alias_ignores_nonmatching_env_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            _ = write_report_worker_task(root, "task.md", managerat="main:0.0")

            result = subprocess.run(
                omo_report_command(agent="agent-4002", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp, manager_target="other:9.0"),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertIn("(pending)", dated_manager_file(root).read_text(encoding="utf-8"))

    def test_omo_report_omo_manager_named_target_routes_to_dated_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            _ = write_report_worker_task(root, "task.md", managerat="omo-manager:0.0")

            result = subprocess.run(
                omo_report_command(agent="agent-4002", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp, manager_target=""),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertIn("(pending)", dated_manager_file(root).read_text(encoding="utf-8"))

    def test_omo_report_configured_main_manager_target_routes_to_dated_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            _ = write_report_worker_task(root, "task.md", managerat="wl:1.0")

            result = subprocess.run(
                omo_report_command(agent="agent-4002", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp, manager_target="wl:1.0"),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertIn("(pending)", dated_manager_file(root).read_text(encoding="utf-8"))

    def test_omo_report_appends_worker_report_to_manager_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("worker done\n", encoding="utf-8")
            manager_task = root / "manager.md"
            manager_task.write_text(task_frontmatter(runat="vl:15", managerat="main:0.0", is_manager=True), encoding="utf-8")
            worker_task = write_report_worker_task(root, "worker.md", runat="vl:2", managerat="vl:15")
            (root / "TODO.md").write_text("current:\nmanager.md vl:15\nworker.md vl:2\n", encoding="utf-8")

            result = subprocess.run(
                omo_report_command(agent="worker-agent", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp, OMO_FAKE_TMUX_INFO="vl\t2\t0\t%report\tworker\n"),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            manager_text = manager_task.read_text(encoding="utf-8")
            self.assertIn("(pending)", manager_text)
            self.assertRegex(manager_text, re.compile(r"^\(from agent vl:2 /tmp/omo-agent-messages-\d+/worker-agent_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))
            self.assertNotIn("(pending)", worker_task.read_text(encoding="utf-8"))
            self.assertFalse(dated_manager_file(root).exists())
            report_text = agent_pointer_paths(manager_text)[0].read_text(encoding="utf-8")
            expected_key = hashlib.sha256(b"\0".join([b"worker done\n", b"worker-agent", b"done", b"vl:2", str(worker_task.resolve(strict=False)).encode()])).hexdigest()
            self.assertTrue(str(agent_pointer_paths(manager_text)[0]).endswith(f"worker-agent_done_{expected_key}.md"))
            assert_concise_agent_report(self, report_text, agent="worker-agent", tmux="vl:2", task_file="worker.md", message=b"worker done\n")

    def test_omo_report_escalates_missing_manager_task_to_main_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("worker done\n", encoding="utf-8")
            worker_task = write_report_worker_task(root, "worker.md", runat="vl:2", managerat="vl:15")
            (root / "TODO.md").write_text("current:\nworker.md vl:2\n", encoding="utf-8")

            result = subprocess.run(
                omo_report_command(agent="worker-agent", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp, OMO_FAKE_TMUX_INFO="vl\t2\t0\t%report\tworker\n"),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            manager_text = dated_manager_file(root).read_text(encoding="utf-8")
            self.assertIn("(pending)", manager_text)
            self.assertRegex(manager_text, re.compile(r"^\(from agent vl:2 /tmp/omo-agent-messages-\d+/worker-agent_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))
            self.assertNotIn("(pending)", worker_task.read_text(encoding="utf-8"))
            report_text = agent_pointer_paths(manager_text)[0].read_text(encoding="utf-8")
            self.assertIn("route-warning:\nTarget manager `vl:15` has no active manager task file. Main manager: find where that manager moved or reassign this report.\nmessage:\nworker done\n", report_text)

    def test_omo_report_keeps_escalated_report_file_when_manager_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("worker done\n", encoding="utf-8")
            _ = write_report_worker_task(root, "worker.md", runat="vl:2", managerat="vl:15")
            (root / "TODO.md").write_text("current:\nworker.md vl:2\n", encoding="utf-8")
            env = report_test_env(tmp, OMO_FAKE_TMUX_INFO="vl\t2\t0\t%report\tworker\n")

            first = subprocess.run(
                omo_report_command(agent="worker-agent", message_file=msg),
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", first.stderr)
            self.assertEqual(0, first.returncode)
            main_report_path = agent_pointer_paths(dated_manager_file(root).read_text(encoding="utf-8"))[0]
            self.assertIn("route-warning:\nTarget manager `vl:15` has no active manager task file.", main_report_path.read_text(encoding="utf-8"))

            manager_task = root / "manager.md"
            manager_task.write_text(task_frontmatter(runat="vl:15", managerat="main:0.0", is_manager=True), encoding="utf-8")
            (root / "TODO.md").write_text("current:\nmanager.md vl:15\nworker.md vl:2\n", encoding="utf-8")
            second = subprocess.run(
                omo_report_command(agent="worker-agent", message_file=msg),
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", second.stderr)
            self.assertEqual(0, second.returncode)
            submanager_report_path = agent_pointer_paths(manager_task.read_text(encoding="utf-8"))[0]
            self.assertNotEqual(main_report_path, submanager_report_path)
            self.assertIn("route-warning:\nTarget manager `vl:15` has no active manager task file.", main_report_path.read_text(encoding="utf-8"))
            self.assertNotIn("route-warning:", submanager_report_path.read_text(encoding="utf-8"))

    def test_omo_report_inferred_main_manager_task_routes_to_dated_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("main manager done\n", encoding="utf-8")
            manager_task = root / "main-manager.md"
            manager_task.write_text(task_frontmatter(runat="agent:1", managerat="main:0.0", is_manager=True), encoding="utf-8")
            write_report_todo(root, manager_task)

            result = subprocess.run(
                omo_report_command(agent="manager-agent", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            manager_text = dated_manager_file(root).read_text(encoding="utf-8")
            self.assertIn("(pending)", manager_text)
            report_text = agent_pointer_paths(manager_text)[0].read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="manager-agent", tmux="agent:1", task_file="main-manager.md", message=b"main manager done\n")

    def test_omo_report_same_body_different_task_keeps_task_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("same done\n", encoding="utf-8")
            for idx, task_name in enumerate(("task-a.md", "task-b.md"), start=1):
                write_report_worker_task(root, task_name, runat=f"agent:{idx}")
                result = subprocess.run(
                    omo_report_command(agent="agent-4002", message_file=msg),
                    cwd=tmp,
                    env=report_test_env(tmp, OMO_FAKE_TMUX_INFO=f"agent\t{idx}\t0\t%report\tworker\n"),
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            report_paths = agent_pointer_paths(dated_manager_file(root).read_text(encoding="utf-8"))
            self.assertEqual(2, len(report_paths))
            self.assertNotEqual(report_paths[0], report_paths[1])
            report_texts = [path.read_text(encoding="utf-8") for path in report_paths]
            self.assertTrue(any("task-file=task-a.md" in text for text in report_texts))
            self.assertTrue(any("task-file=task-b.md" in text for text in report_texts))

    def test_omo_report_rejects_corrupt_existing_tmp_pointer_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            write_report_worker_task(root)
            env = report_test_env(tmp)
            base_cmd = omo_report_command(agent="agent-4002", message_file=msg)
            first = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual("", first.stderr)
            self.assertEqual(0, first.returncode)
            report_path = agent_pointer_paths(dated_manager_file(root).read_text(encoding="utf-8"))[0]
            report_path.write_text("wrong\n", encoding="utf-8")
            second = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertNotEqual(0, second.returncode)
            self.assertIn("stale or corrupt report file", second.stderr)

    def test_omo_report_rewrites_existing_verbose_tmp_pointer_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            task = write_report_worker_task(root)
            env = report_test_env(tmp)
            base_cmd = omo_report_command(agent="agent-4002", message_file=msg)
            first = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual("", first.stderr)
            self.assertEqual(0, first.returncode)
            manager_log = dated_manager_file(root)
            report_path = agent_pointer_paths(manager_log.read_text(encoding="utf-8"))[0]
            message_hash = hashlib.sha256(b"done\n").hexdigest()
            report_path.write_text(
                "\n".join(
                    [
                        "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done]",
                        "(from agent agent-4002 via omo_report.sh status=done)",
                        f"(report manager 2026-06-10 12:02 agent=agent-4002 status=done report-file={report_path})",
                        f"[message-sha256: {message_hash}]",
                        f"message-file: {report_path}",
                        f"task-file: {task}",
                        f"task-pointer: (from agent agent-4002 {report_path})",
                        "message:",
                        "done",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            second = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual("", second.stderr)
            self.assertEqual(0, second.returncode)
            self.assertEqual(1, manager_log.read_text(encoding="utf-8").count("(pending)"))
            assert_concise_agent_report(self, report_path.read_text(encoding="utf-8"), agent="agent-4002", tmux="agent:1", task_file="task.md")

    def test_omo_report_deduplicates_old_format_pending_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            _ = msg.write_text("done\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"done\n").hexdigest()
            write_report_worker_task(root)
            manager_log = dated_manager_file(root)
            manager_log.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done tmux_session=agent tmux_window_index=1 tmux_pane_index=0 tmux_pane_id=%report tmux_target=agent:1.0 tmux_window_name=report-window]",
                        "(from agent agent-4002 via omo_report.sh status=done)",
                        "(report manager 2026-06-10 12:02 agent=agent-4002 status=done)",
                        f"[message-sha256: {old_hash}]",
                        "message-file: /tmp/msg.md",
                        "message:",
                        "> done",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                omo_report_command(agent="agent-4002", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            text = manager_log.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("(pending)"))
            self.assertEqual(1, text.count("[message-sha256: "))

    def test_omo_report_ignores_routed_block_when_deduplicating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            _ = msg.write_text("done\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"done\n").hexdigest()
            write_report_worker_task(root)
            manager_log = dated_manager_file(root)
            manager_log.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "(manager routed: to `done.md`.)",
                        "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done]",
                        "(from agent agent-4002 via omo_report.sh status=done)",
                        f"[message-sha256: {old_hash}]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                omo_report_command(agent="agent-4002", message_file=msg),
                cwd=tmp,
                env=report_test_env(tmp),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            text = manager_log.read_text(encoding="utf-8")
            self.assertEqual(2, text.count("(pending)"))
            self.assertEqual(1, text.count("[message-sha256: "))
            self.assertRegex(text, re.compile(r"^\(from agent agent:1 /tmp/omo-agent-messages-\d+/agent-4002_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))

    def test_omo_report_old_format_tmux_route_dedupes_only_same_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\ncase \"$*\" in\n  *%1701*) printf 'cfg\\t7\\t0\\t%%1701\\tleft\\n' ;;\n  *%1702*) printf 'cfg\\t7\\t1\\t%%1702\\tright\\n' ;;\n  *) exit 1 ;;\nesac\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"same body\n").hexdigest()
            write_report_worker_task(root, runat="cfg:7")
            manager_log = dated_manager_file(root)
            manager_log.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "[omo-message-source: origin=agent agent=agent-tmux via=omo_report.sh status=done tmux_session=cfg tmux_window_index=7 tmux_pane_index=0 tmux_pane_id=%1701 tmux_target=cfg:7.0 tmux_window_name=left]",
                        "(from agent agent-tmux via omo_report.sh status=done)",
                        f"[message-sha256: {old_hash}]",
                        "message:",
                        "> same body",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            base_env = {
                **report_test_env(tmp),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            }
            for pane, expected_blocks in (("%1701", 1), ("%1702", 2)):
                result = subprocess.run(
                    omo_report_command(agent="agent-tmux", message_file=msg),
                    cwd=tmp,
                    env={**base_env, "TMUX_PANE": pane},
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
                self.assertEqual(expected_blocks, manager_log.read_text(encoding="utf-8").count("(pending)"))

    def test_omo_report_old_format_tmux_route_dedupes_after_window_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\nprintf 'cfg\\t7\\t0\\t%%1701\\tright\\n'\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"same body\n").hexdigest()
            write_report_worker_task(root, runat="cfg:7")
            manager_log = dated_manager_file(root)
            manager_log.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "[omo-message-source: origin=agent agent=agent-tmux via=omo_report.sh status=done tmux_session=cfg tmux_window_index=7 tmux_pane_index=0 tmux_pane_id=%old tmux_target=cfg:7.0 tmux_window_name=left]",
                        "(from agent agent-tmux via omo_report.sh status=done)",
                        f"[message-sha256: {old_hash}]",
                        "message:",
                        "> same body",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                omo_report_command(agent="agent-tmux", message_file=msg),
                cwd=tmp,
                env={
                    **report_test_env(tmp),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual(1, manager_log.read_text(encoding="utf-8").count("(pending)"))

    def test_omo_report_requires_message_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            result = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_report.sh"),
                    "--status",
                    "done",
                    "--agent",
                    "agent-file-required",
                ],
                cwd=tmp,
                env=report_test_env(tmp),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("--message-file", result.stderr)
            self.assertFalse((root / "task.md").exists())

    def test_omo_report_derives_tmux_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\nprintf 'cfg\\t7\\t0\\t%%1701\\tmanager window\\n'\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("tmux report\n", encoding="utf-8")
            write_report_worker_task(root, runat="cfg:7")
            result = subprocess.run(
                omo_report_command(agent="agent-tmux", message_file=msg),
                cwd=tmp,
                env={
                    **report_test_env(tmp),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            text = dated_manager_file(root).read_text(encoding="utf-8")
            self.assertRegex(text, re.compile(r"^\(from agent cfg:7 /tmp/omo-agent-messages-\d+/agent-tmux_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))
            self.assertNotIn("tmux_session=cfg", text)
            self.assertNotIn("message-file: ", text)
            report_paths = agent_pointer_paths(text)
            self.assertEqual(1, len(report_paths))
            report_text = report_paths[0].read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="agent-tmux", tmux="cfg:7", task_file="task.md")
            self.assertNotIn("tmux_session=", report_text)
            self.assertNotIn("tmux_window_index=", report_text)
            self.assertNotIn("tmux_pane_index=", report_text)

    def test_omo_report_tmux_window_rename_updates_tmp_without_duplicate_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\nprintf 'cfg\\t7\\t0\\t%%1701\\t%s\\n' \"$OMO_FAKE_WINDOW_NAME\"\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            write_report_worker_task(root, runat="cfg:7")
            base_env = {
                **report_test_env(tmp),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMUX_PANE": "%1701",
            }
            for window_name in ("left", "right"):
                result = subprocess.run(
                    omo_report_command(agent="agent-tmux", message_file=msg),
                    cwd=tmp,
                    env={**base_env, "OMO_FAKE_WINDOW_NAME": window_name},
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            text = dated_manager_file(root).read_text(encoding="utf-8")
            self.assertEqual(1, text.count("(pending)"))
            report_paths = agent_pointer_paths(text)
            self.assertEqual(1, len(report_paths))
            report_text = report_paths[0].read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="agent-tmux", tmux="cfg:7", task_file="task.md")

    def test_omo_report_same_hash_different_tmux_routes_append_distinct_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\ncase \"$*\" in\n  *%1701*) printf 'cfg\\t7\\t0\\t%%1701\\tleft\\n' ;;\n  *%1702*) printf 'cfg\\t7\\t1\\t%%1702\\tright\\n' ;;\n  *) exit 1 ;;\nesac\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            write_report_worker_task(root, runat="cfg:7")
            for pane in ("%1701", "%1702"):
                result = subprocess.run(
                    omo_report_command(agent="agent-tmux", message_file=msg),
                    cwd=tmp,
                    env={
                        **report_test_env(tmp),
                        "PATH": f"{bin_dir}:{os.environ['PATH']}",
                        "TMUX_PANE": pane,
                    },
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            text = dated_manager_file(root).read_text(encoding="utf-8")
            self.assertEqual(2, text.count("(pending)"))
            self.assertEqual(0, text.count("message-file: "))
            self.assertIn("(from agent cfg:7 ", text)
            self.assertIn("(from agent cfg:7.1 ", text)
            report_text = "\n".join(path.read_text(encoding="utf-8") for path in agent_pointer_paths(text))
            self.assertIn("tmux=cfg:7 ", report_text)
            self.assertIn("tmux=cfg:7.1 ", report_text)
            self.assertNotIn("tmux_target=", report_text)
            self.assertNotIn("message:\n", text)

    def test_omo_report_concurrent_distinct_reports_keep_all_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            messages = []
            for idx in range(12):
                msg = Path(tmp) / f"msg-{idx}.md"
                _ = msg.write_text(f"distinct report {idx}\n", encoding="utf-8")
                messages.append(msg)
            write_report_worker_task(root)
            env = report_test_env(tmp)
            procs = [
                subprocess.Popen(
                    omo_report_command(agent="agent-4002", message_file=msg),
                    cwd=tmp,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for msg in messages
            ]
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=20)
                self.assertEqual("", stdout)
                self.assertEqual("", stderr)
                self.assertEqual(0, proc.returncode)
            manager_log = dated_manager_file(root)
            text = manager_log.read_text(encoding="utf-8")
            self.assertEqual(12, text.count("(from agent agent:1 "))
            self.assertEqual(0, text.count("message-file: "))
            self.assertNotIn("message:\n", text)
            for idx in range(12):
                self.assertNotIn(f"> distinct report {idx}", text)
            report_paths = agent_pointer_paths(text)
            self.assertEqual(12, len(report_paths))
            report_bodies = sorted(path.read_text(encoding="utf-8").rsplit("message:\n", 1)[1] for path in report_paths)
            self.assertEqual(sorted(f"distinct report {idx}\n" for idx in range(12)), report_bodies)
            self.assertEqual(12, len(find_markers(root, [manager_log])))

    def test_new_email_source_is_delivered_by_pending_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: routed\n\nPlease handle this.\n", encoding="utf-8")
            path = dated_manager_file(root)
            _ = path.write_text("(pending)\n(from email manager_mail/4002.txt)\n[source: email manager_mail/4002.txt]\n", encoding="utf-8")
            seen: dict[str, float] = {}
            args = Args(
                root=root,
                manager_url="",
                state=Path(tmp) / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=True,
            )
            from omo_manager.omo_pending_watch import scan_once

            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, seen, [path]))
            text = out.getvalue()
            self.assertIn("Normally record pending items and remove the consumed `(pending)` marker by running:", text)
            self.assertIn("--ack-human", text)
            self.assertIn("--email-file manager_mail/4002.txt", text)
            self.assertIn("Choose `--item` values by quoting the human's words as much as possible.", text)
            self.assertIn("<snippet file=\"work_manager_", text)
            self.assertIn("(from email manager_mail/4002.txt)", text)

    def test_record_and_delegate_email_source_is_delivered_by_pending_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dated_manager_file(root)
            _ = path.write_text("(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("human", markers[0].origin)
            self.assertEqual("email", markers[0].source)
            self.assertIn("(delegate manager_mail/4002.txt)", markers[0].ref)

    def test_pending_watch_can_add_manager_policy_reminder(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: reminder\n\nPlease handle this.\n", encoding="utf-8")
            path = dated_manager_file(root)
            path.write_text("(pending)\n(from email manager_mail/4002.txt)\n", encoding="utf-8")
            args = Args(
                root=root,
                manager_url="",
                state=root / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=True,
                reminder_random=lambda: 0.0,
                reminder_choice=lambda reminders: reminders[1],
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("Normally record pending items and remove the consumed `(pending)` marker by running:", text)
            self.assertIn("--ack-human", text)
            self.assertIn("Reminder: stay high level; route concrete work to agents.", text)

    def test_pending_watch_skips_manager_policy_reminder_when_not_selected(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(
                root=root,
                manager_url="",
                state=root / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=True,
                reminder_random=lambda: 1.0,
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("Normally record pending items and remove the consumed `(pending)` marker by running:", text)
            self.assertIn("--ack-human", text)
            self.assertNotIn("Reminder:", text)

    def test_email_pending_ref_can_add_email_policy_reminder(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: email reminder\n\nPlease handle this.\n", encoding="utf-8")
            path = dated_manager_file(root)
            path.write_text("(pending)\n(from email manager_mail/4002.txt)\n", encoding="utf-8")
            args = Args(
                root=root,
                manager_url="",
                state=root / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=True,
                reminder_random=lambda: 0.0,
                reminder_choice=lambda reminders: reminders[-1],
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertIn("Reminder: acknowledge human email first, then delegate.", out.getvalue())

    def test_oversized_pending_task_file_output_includes_continuation_warning(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            lines = ["(pending)", "please route", *["history" for _ in range(1999)]]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("<snippet file=\"task.md:1-2001\">", text)
            self.assertRegex(text, r"…\d+chars…")

    def test_normal_size_pending_task_file_output_has_no_continuation_warning(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("<snippet file=\"task.md:1-2\">", text)
            self.assertNotIn("task-file length warning", text)

    def test_pending_push_includes_pending_tail_and_truncates_it(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            body = "x" * (watcher.PENDING_CONTENT_CHAR_LIMIT + 50)
            path.write_text(f"header\n(pending)\n{body}\nsecret-tail\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("<snippet file=\"task.md:2-4\">\n(pending)\n", text)
            self.assertRegex(text, r"…\d+chars…")
            self.assertIn("secret-tail", text)
            self.assertIn("Normally record pending items and remove the consumed `(pending)` marker by running:", text)
            self.assertIn("--ack-human", text)

    def test_email_pending_push_attaches_email_content(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: hello\n\nPlease inspect this.\n", encoding="utf-8")
            path = root / "work_manager_today.md"
            path.write_text("(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("<snippet file=\"manager_mail/4002.txt:1-3\">", text)
            self.assertIn("Subject: hello", text)
            self.assertIn("Please inspect this.", text)

    def test_pending_push_attaches_referenced_file_content(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            docs = root / "docs"
            docs.mkdir()
            ref = docs / "request.md"
            ref.write_text("line one\nline two\n", encoding="utf-8")
            path = root / "task.md"
            path.write_text("(pending)\nsee docs/request.md\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("<snippet file=\"docs/request.md:1-2\">", text)
            self.assertIn("line one\nline two", text)

    def test_relative_referenced_file_cannot_escape_root(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "root"
            root.mkdir()
            outside = parent / "outside.txt"
            outside.write_text("outside line\n", encoding="utf-8")
            path = root / "task.md"
            path.write_text("(pending)\nsee ../outside.txt\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertFalse(watcher.scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("<source-error file=\"../outside.txt\">relative source escapes root</source-error>", text)
            self.assertNotIn("outside line", text)

    def test_absolute_referenced_file_must_be_agent_message(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("outside line\n", encoding="utf-8")
            path = root / "task.md"
            path.write_text(f"(pending)\nsee {outside}\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertFalse(watcher.scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn(f"<source-error file=\"{outside}\">absolute source is not an agent message file</source-error>", text)
            self.assertNotIn("outside line", text)

    def test_agent_pending_push_attaches_agent_message_content(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-", dir="/tmp") as msg_tmp:
            root = Path(tmp)
            report = Path(msg_tmp) / "agent_running_657689.md"
            report.write_text(
                "(sent from agent via omo_report.sh tmux=hcfg:1 time=11:08 task-file=helper_audit_agent_9580.md)\n"
                "[message-sha256: 657689]\n"
                "message:\n"
                "Please have the manager fix this.\n",
                encoding="utf-8",
            )
            path = root / "helper_audit_agent_9580.md"
            path.write_text(f"(pending)\n(from agent hcfg:1 {report})\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("Normally record pending items and remove the consumed `(pending)` marker by running:", text)
            self.assertIn("Do not pass `--ack-human`", text)
            self.assertNotIn("Use `--ack-human`", text)
            self.assertIn(f"<snippet file=\"helper_audit_agent_9580.md:1-2\">\n(pending)\n(from agent {report})", text)
            self.assertNotIn(f"(from agent hcfg:1 {report})", text)
            self.assertIn(f"<snippet file=\"{report}:1-4\">", text)
            self.assertIn("Please have the manager fix this.", text)

    def test_email_dm_pending_pushes_worker_and_manager_fyi(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: worker note\n\nPlease inspect this directly. DM!!!\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(2, len(calls))
            self.assertEqual("wl:2", calls[0][calls[0].index("--manager-target") + 1])
            self.assertEqual("wl:1", calls[1][calls[1].index("--manager-target") + 1])
            self.assertEqual("Subject: worker note\n\nPlease inspect this directly.", calls[0][1])
            self.assertNotIn("Immediately record", calls[0][1])
            self.assertIn("Normally record pending items and remove the consumed `(pending)` marker by running:", calls[1][1])
            self.assertIn("this message is already dispatched to the agent, this is FYI", calls[1][1])
            self.assertIn("--ack-human", calls[1][1])
            self.assertIn("--email-file manager_mail/4002.txt", calls[1][1])
            self.assertNotIn("DM!!!", calls[0][1])
            self.assertNotIn('<snippet file="worker.md:', calls[0][1])
            self.assertIn('<snippet file="worker.md:', calls[1][1])
            self.assertNotIn("[omo-message-source:", calls[0][1])
            self.assertNotIn("[omo-message-source:", calls[1][1])

    def test_email_dm_async_worker_launch_does_not_mark_seen(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            sends: list[tuple[str, str]] = []

            def fake_send_to_codex(target: str, message: str, _options: object = None, **_kwargs: object) -> object | None:
                sends.append((target, message))
                return object() if target == "wl:2" else None

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=fake_send_to_codex):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertEqual(["wl:2", "wl:1"], [target for target, _message in sends])
            self.assertEqual({}, seen)

    def test_pending_block_dm_pushes_worker_without_task_file_snippet(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: worker note\n\nPlease inspect this directly.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\nDM\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Subject: worker note\n\nPlease inspect this directly.", calls[0][1])
            self.assertNotIn("\n(pending)\n", calls[0][1])
            self.assertNotIn('<snippet file="worker.md:', calls[0][1])
            self.assertIn('<snippet file="worker.md:', calls[1][1])

    def test_pending_block_dm_prefix_pushes_worker_without_task_file_snippet(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: worker note\n\nPlease inspect this directly.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\nDM: please inspect directly\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("please inspect directly\n\nSubject: worker note\n\nPlease inspect this directly.", calls[0][1])
            self.assertNotIn("DM: please inspect directly", calls[0][1])
            self.assertNotIn('<snippet file="worker.md:', calls[0][1])
            self.assertIn('<snippet file="worker.md:', calls[1][1])

    def test_pending_block_dm_only_pushes_worker_without_manager_fyi(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: worker note\n\nPlease inspect this directly.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\nDM only: please inspect directly\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("please inspect directly\n\nSubject: worker note\n\nPlease inspect this directly.", calls[0][1])
            self.assertNotIn("DM only: please inspect directly", calls[0][1])
            self.assertNotIn("this message is already dispatched to the agent, this is FYI", calls[0][1])
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("(pending)", text)
            self.assertIn("DM only: please inspect directly", text)

    def test_pending_block_dm_only_strips_case_and_punctuation_for_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: worker note\n\nPlease inspect this directly.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\nDM Only. Please inspect directly.\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Please inspect directly.\n\nSubject: worker note\n\nPlease inspect this directly.", calls[0][1])
            self.assertNotIn("(pending)", calls[0][1])
            self.assertNotIn("DM Only", calls[0][1])
            self.assertNotIn("(pending)", path.read_text(encoding="utf-8"))

    def test_pending_block_dm_only_preserves_quoted_content_for_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\nDM only\n> quoted error excerpt\nPlease inspect directly.\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual("> quoted error excerpt\nPlease inspect directly.", calls[0][1])
            self.assertNotIn("\nDM only\n", calls[0][1])
            self.assertNotIn("\n(pending)\n", calls[0][1])

    def test_pending_block_dm_only_strips_manager_source_marker_for_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        self.assertEqual("Please inspect directly.", watcher.clean_direct_message_lines("(pending)\nDM only\n(from manager omo_task_edit delegate-message)\nPlease inspect directly."))
        self.assertEqual(("agent", "manager"), watcher.marker_origin_source(["(pending)", "DM only", "(from manager omo_task_edit delegate-message)", "Please inspect directly."]))

    def test_direct_marker_stripping_preserves_same_line_payload_syntax(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        self.assertEqual("- [ ] fix task", watcher.strip_direct_markers("(pending)\nDM - [ ] fix task"))
        self.assertEqual("- [ ] fix task", watcher.strip_direct_markers("(pending)\nDM: - [ ] fix task"))
        self.assertEqual("> quoted payload", watcher.strip_direct_markers("(pending)\nDM > quoted payload"))
        self.assertEqual("(for manager: ask agent to report back)", watcher.strip_direct_markers("(pending)\nDM (for manager: ask agent to report back)"))
        self.assertEqual("- [ ] fix task", watcher.strip_direct_markers("(pending)\n- [ ] fix task DM only."))
        self.assertEqual("payload", watcher.strip_direct_markers("(pending)\nDM\npayload\nDM"))
        self.assertEqual("payload", watcher.strip_direct_markers("(pending)\nDM only\npayload\nDM only"))
        self.assertEqual("please DM\nmore details", watcher.strip_direct_markers("(pending)\nDM\nplease DM\nmore details"))
        self.assertEqual("payload", watcher.strip_direct_markers("(pending)\n(DM)\npayload"))
        self.assertEqual("payload", watcher.strip_direct_markers("(pending)\n[DM]\npayload"))
        self.assertEqual("payload", watcher.strip_direct_markers("(pending)\npayload (DM)"))

    def test_linked_file_dm_only_pushes_worker_without_manager_fyi(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("Follow this linked request. DM only\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\ndocs/request.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Follow this linked request.", calls[0][1])
            self.assertNotIn('<snippet file="docs/request.md:1-1">', calls[0][1])
            self.assertNotIn("Follow this linked request. DM only", calls[0][1])
            self.assertNotIn('<snippet file="worker.md:', calls[0][1])
            self.assertNotIn("this message is already dispatched to the agent, this is FYI", calls[0][1])
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("(pending)", text)
            self.assertIn("docs/request.md", text)

    def test_linked_file_dm_only_preserves_quoted_content_for_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("DM only\n> quoted file excerpt\nFollow this linked request.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\ndocs/request.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual("> quoted file excerpt\nFollow this linked request.", calls[0][1])
            self.assertNotIn("\nDM only\n", calls[0][1])

    def test_dm_only_clears_one_pending_marker_per_delivery(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(
                f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n"
                "(pending)\nDM only: first\n"
                "(pending)\nDM only: second\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertEqual(["wl:2", "wl:2"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertIn("first", calls[0][1])
            self.assertNotIn("DM only", calls[0][1])
            self.assertNotIn("second", calls[0][1])
            self.assertIn("second", calls[1][1])
            self.assertNotIn("DM only", calls[1][1])
            self.assertNotIn("(pending)", path.read_text(encoding="utf-8"))

    def test_agent_origin_dm_manager_fyi_says_do_not_ack_human(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(from agent wl:9 /tmp/omo-agent-messages-test/request.md)\nDM\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertIn("Do not pass `--ack-human`", calls[1][1])
            self.assertIn("this message is already dispatched to the agent, this is FYI", calls[1][1])
            self.assertNotIn("Use `--ack-human`", calls[1][1])

    def test_agent_report_dm_sends_only_report_message_body_to_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-", dir="/tmp") as msg_tmp:
            root = Path(tmp)
            report = Path(msg_tmp) / "request.md"
            report.write_text(
                "(sent from agent via omo_report.sh tmux=hcfg:1 time=11:08 task-file=task.md)\n"
                "[message-sha256: abc]\n"
                "message:\n"
                "Please inspect this directly. DM\n",
                encoding="utf-8",
            )
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(from agent wl:9 {report})\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Please inspect this directly.", calls[0][1])
            self.assertNotIn("message-sha256", calls[0][1])
            self.assertNotIn("sent from agent", calls[0][1])

    def test_linked_file_dm_strips_source_marker_lines_from_attachment(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.txt"
            request.write_text(
                "(from email manager_mail/1.txt)\n"
                "[source: email manager_mail/1.txt]\n"
                "(from agent wl:9 /tmp/omo-agent-messages-test/request.md)\n"
                "(pending)\n"
                "DM only\n"
                "Please inspect this directly.\n"
                "DM\n",
                encoding="utf-8",
            )
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\ndocs/request.txt\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Please inspect this directly.", calls[0][1])
            self.assertNotIn("manager_mail/1.txt", calls[0][1])
            self.assertNotIn("from agent", calls[0][1])

    def test_quoted_dm_in_pending_block_does_not_trigger_dm(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: manager note\n\nPlease route this through the manager.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n> DM\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertNotIn("Direct message from the human", calls[0][1])

    def test_linked_file_dm_pushes_worker_even_when_delegate_mail_is_not_dm(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: worker note\n\nPlease inspect this directly.\n", encoding="utf-8")
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("Follow this linked request. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\ndocs/request.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Subject: worker note\n\nPlease inspect this directly.\n\nFollow this linked request.", calls[0][1])
            self.assertNotIn('<snippet file="docs/request.md:1-1">', calls[0][1])
            self.assertNotIn('<snippet file="worker.md:', calls[0][1])
            self.assertIn('<snippet file="worker.md:', calls[1][1])

    def test_linked_file_dm_prefix_pushes_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("DM: follow this linked request.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\ndocs/request.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("follow this linked request.", calls[0][1])
            self.assertNotIn('<snippet file="docs/request.md:1-1">', calls[0][1])
            self.assertNotIn('<snippet file="worker.md:', calls[0][1])
            self.assertIn('<snippet file="worker.md:', calls[1][1])

    def test_linked_file_dm_decorated_pointer_sends_only_file_content_to_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("Follow this linked request. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n- docs/request.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Follow this linked request.", calls[0][1])
            self.assertNotIn("docs/request.md", calls[0][1])

    def test_linked_file_dm_markdown_link_pointer_sends_only_file_content_to_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("Follow this linked request. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n[request](docs/request.md)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Follow this linked request.", calls[0][1])
            self.assertNotIn("[request]", calls[0][1])

    def test_linked_file_dm_markdown_link_pointer_ignores_title_and_wrapper(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            first = docs / "first.md"
            second = docs / "second.md"
            first.write_text("Follow the first linked request. DM\n", encoding="utf-8")
            second.write_text("Follow the second linked request. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(
                f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n"
                "(pending)\n"
                '[first](docs/first.md "title")\n'
                "([second](docs/second.md))\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("Follow the first linked request.\n\nFollow the second linked request.", calls[0][1])
            self.assertNotIn("[first]", calls[0][1])
            self.assertNotIn("[second]", calls[0][1])

    def test_quoted_dm_in_linked_file_does_not_trigger_dm(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("> DM\nRegular manager-routed request.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\ndocs/request.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertNotIn("Direct message from the human", calls[0][1])
            self.assertIn('<snippet file="docs/request.md:1-2">', calls[0][1])

    def test_quoted_link_to_dm_file_does_not_trigger_dm(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            request = docs / "request.md"
            request.write_text("DM: old quoted request.\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n> docs/request.md\nPlease route this normally.\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertNotIn("Direct message from the human", calls[0][1])
            self.assertNotIn('<snippet file="docs/request.md:', calls[0][1])

    def test_later_pending_block_dm_link_does_not_make_earlier_block_dm(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            first = docs / "first.md"
            second = docs / "second.md"
            first.write_text("Regular manager-routed request.\n", encoding="utf-8")
            second.write_text("Direct worker request. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(
                f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n"
                "(pending)\n"
                "docs/first.md\n"
                "(pending)\n"
                "docs/second.md\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:1", "wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertIn('<snippet file="docs/first.md:1-1">', calls[0][1])
            self.assertIn('<snippet file="worker.md:', calls[0][1])
            self.assertEqual("Direct worker request.", calls[1][1])
            self.assertNotIn('<snippet file="docs/second.md:1-1">', calls[1][1])
            self.assertNotIn('<snippet file="worker.md:', calls[1][1])

    def test_email_dm_uses_frontmatter_runat_over_body_runat(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\nrunat: wl:9 codex\n(done: old worker)\n\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])

    def test_email_dm_uses_frontmatter_runat_when_body_runat_follows_pending(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\nrunat: wl:9 codex\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])

    def test_email_dm_without_frontmatter_ignores_legacy_runat(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text("runat: wl:2 codex\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="wl:1"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(1, len(calls))
            self.assertEqual("wl:1", calls[0][calls[0].index("--manager-target") + 1])
            self.assertIn("no safe worker `runat:` target was found", calls[0][1])

    def test_email_dm_manager_fyi_survives_worker_clearing_marker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                target = delivery_target(command)
                if target == "wl:2":
                    path.write_text(f"{task_frontmatter(status='done', runat='wl:2', managerat='wl:1')}\n(done)\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(2, len(calls))
            self.assertEqual("wl:1", calls[1][calls[1].index("--manager-target") + 1])
            self.assertNotIn("--pending-file", calls[1])
            self.assertIn("this message is already dispatched to the agent, this is FYI", calls[1][1])
            self.assertIn("Use `--ack-human`", calls[1][1])

    def test_email_dm_uses_managerat_without_global_manager_target(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2.0', managerat='wl:1.0')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2.0", "wl:1.0"], [call[call.index("--manager-target") + 1] for call in calls])

    def test_email_dm_rejects_ambiguous_same_window_target(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:2.1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(1, len(calls))
            self.assertEqual("wl:2.1", calls[0][calls[0].index("--manager-target") + 1])
            self.assertIn("no safe worker `runat:` target was found", calls[0][1])

    def test_email_dm_worker_failure_sends_manager_action_fallback(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                target = delivery_target(command)
                return subprocess.CompletedProcess(command, 2 if target == "wl:2" else 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertEqual(1, len(seen))
            self.assertEqual(2, len(calls))
            self.assertEqual("wl:2", calls[0][calls[0].index("--manager-target") + 1])
            self.assertEqual("wl:1", calls[1][calls[1].index("--manager-target") + 1])
            manager_text = calls[1][1]
            self.assertIn("direct worker delivery to wl:2 failed with status 2", manager_text)
            self.assertIn("manager action required to route or help", manager_text)
            self.assertNotIn("delivered directly to worker target wl:2", manager_text)
            self.assertNotIn("no manager action required", manager_text)

    def test_email_dm_with_secondary_attachment_error_still_dispatches_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\nsee missing.md\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertEqual("see missing.md\n\nPlease inspect this directly.", calls[0][1])
            self.assertNotIn("<source-error", calls[0][1])

    def test_email_dm_worker_launch_failure_sends_manager_action_fallback(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                target = delivery_target(command)
                if target == "wl:2":
                    raise OSError("exec failed")
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            err = StringIO()
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run), redirect_stderr(err):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                self.assertFalse(watcher.scan_once(args, seen, [path]))
            self.assertEqual(1, len(seen))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertIn("pending delivery failed: exec failed", err.getvalue())

    def test_email_dm_manager_fyi_retry_does_not_redeliver_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []
            manager_attempts = 0

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                nonlocal manager_attempts
                calls.append(capture_delivery_call(command))
                target = delivery_target(command)
                if target == "wl:1":
                    manager_attempts += 1
                    return subprocess.CompletedProcess(command, 1 if manager_attempts == 1 else 0)
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertFalse(watcher.scan_once(args, seen, [path]))
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            targets = [call[call.index("--manager-target") + 1] for call in calls]
            self.assertEqual(["wl:2", "wl:1", "wl:1"], targets)
            self.assertEqual(1, targets.count("wl:2"))
            self.assertEqual(2, targets.count("wl:1"))
            self.assertIn("this message is already dispatched to the agent, this is FYI", calls[1][1])
            self.assertIn("this message is already dispatched to the agent, this is FYI", calls[2][1])

    def test_email_dm_content_change_redelivers_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect the first version. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                mail.write_text("Please inspect the second version. DM\n", encoding="utf-8")
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            targets = [call[call.index("--manager-target") + 1] for call in calls]
            self.assertEqual(["wl:2", "wl:1", "wl:2", "wl:1"], targets)
            self.assertIn("first version", calls[0][1])
            self.assertIn("second version", calls[2][1])

    def test_email_dm_suffix_ignores_unicode_punctuation(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM...\u201d\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(["wl:2", "wl:1"], [call[call.index("--manager-target") + 1] for call in calls])

    def test_email_hidden_tail_dm_change_redelivers_worker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            visible = "x" * watcher.EMAIL_CONTENT_CHAR_LIMIT
            mail.write_text(f"{visible}\nold hidden\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                mail.write_text(f"{visible}\nDM\n", encoding="utf-8")
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            targets = [call[call.index("--manager-target") + 1] for call in calls]
            self.assertEqual(["wl:1", "wl:2", "wl:1"], targets)
            self.assertEqual(visible, calls[1][1])

    def test_email_dm_manager_fyi_skips_policy_reminder(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please inspect this directly. DM\n", encoding="utf-8")
            path = root / "worker.md"
            path.write_text(f"{task_frontmatter(runat='wl:2', managerat='wl:1')}\n(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root,
                manager_url="",
                state=root / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=False,
                manager_target="main:0.0",
                reminder_random=lambda: 0.0,
                reminder_choice=lambda reminders: reminders[-1],
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            manager_text = calls[1][1]
            self.assertIn("this message is already dispatched to the agent, this is FYI", manager_text)
            self.assertIn("Use `--ack-human`", manager_text)
            self.assertNotIn("Reminder:", manager_text)
            self.assertNotIn("acknowledge human email first, then delegate", manager_text)

    def test_email_dm_pending_falls_back_when_worker_target_is_unknown(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Please route this. DM.\n", encoding="utf-8")
            path = root / "work_manager_today.md"
            path.write_text("(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.scan_once(args, {}, [path]))
            self.assertEqual(1, len(calls))
            self.assertEqual("main:0.0", calls[0][calls[0].index("--manager-target") + 1])
            self.assertIn("direct-message fallback: pending block or linked file starts or ends with `DM`, but no safe worker `runat:` target was found; delivering to the manager for routing.", calls[0][1])

    def test_email_attachment_error_does_not_mark_marker_seen(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager_today.md"
            path.write_text("(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=False, manager_target="main:0.0"
            )
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertFalse(watcher.scan_once(args, seen, [path]))
                self.assertFalse(watcher.scan_once(args, seen, [path]))
                mail = root / "manager_mail" / "4002.txt"
                mail.parent.mkdir()
                mail.write_text("Subject: now readable\n\nPlease inspect this.\n", encoding="utf-8")
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertEqual(2, len(calls))
            self.assertIn("cannot read source", calls[0][1])
            self.assertIn("Subject: now readable", calls[1][1])

    def test_same_process_seen_suppresses_unchanged_pending_marker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nplease handle this\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            calls: list[object] = []

            def fake_push_ref(*call_args: object) -> int:
                calls.append(call_args)
                return 0

            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.push_ref", side_effect=fake_push_ref):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                self.assertFalse(watcher.scan_once(args, seen, [path]))
            self.assertEqual(1, len(calls))

    def test_same_process_seen_redelivers_changed_pending_tail(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nold details\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            calls: list[object] = []

            def fake_push_ref(*call_args: object) -> int:
                calls.append(call_args)
                return 0

            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.push_ref", side_effect=fake_push_ref):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                path.write_text("(pending)\nnew details\n", encoding="utf-8")
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertEqual(2, len(calls))

    def test_same_process_seen_redelivers_changed_truncated_pending_tail(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            head = "h" * watcher.PENDING_CONTENT_CHAR_LIMIT
            tail = "t" * watcher.PENDING_CONTENT_CHAR_LIMIT
            path.write_text(f"(pending)\n{head}\nold hidden middle\n{tail}\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            calls: list[object] = []

            def fake_push_ref(*call_args: object) -> int:
                calls.append(call_args)
                return 0

            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.push_ref", side_effect=fake_push_ref):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                path.write_text(f"(pending)\n{head}\nnew hidden middle\n{tail}\n", encoding="utf-8")
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertEqual(2, len(calls))

    def test_email_attachment_content_change_redelivers_pending_marker(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: first\n\nPlease inspect this.\n", encoding="utf-8")
            path = root / "work_manager_today.md"
            path.write_text("(pending)\n(record and delegate manager_mail/4002.txt)\n", encoding="utf-8")
            args = Args(
                root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True
            )
            seen: dict[str, float] = {}
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
                mail.write_text("Subject: second\n\nPlease inspect this.\n", encoding="utf-8")
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            text = out.getvalue()
            self.assertIn("Subject: first", text)
            self.assertIn("Subject: second", text)

    def test_seen_cache_expires_entries_by_time(self) -> None:
        from omo_manager.omo_pending_watch import new_seen_cache, remember_seen

        seen = new_seen_cache()
        remember_seen(seen, "old", 1.0, ttl_s=10.0)
        remember_seen(seen, "new", 12.0, ttl_s=10.0)
        self.assertNotIn("old", seen)
        self.assertEqual({"new": 12.0}, seen)

    def test_seen_cache_lookup_does_not_refresh_expiry(self) -> None:
        from omo_manager.omo_pending_watch import remember_seen, seen_contains

        seen: dict[str, float] = {}
        remember_seen(seen, "old", 1.0, ttl_s=10.0)
        self.assertTrue(seen_contains(seen, "old", 5.0, ttl_s=10.0))
        self.assertFalse(seen_contains(seen, "old", 11.0, ttl_s=10.0))
        self.assertEqual({}, seen)

    def test_positive_float_env_falls_back_for_invalid_seen_ttl(self) -> None:
        from omo_manager.omo_pending_watch import positive_float_env

        with patch.dict(os.environ, {"OMO_MANAGER_SEEN_TTL_S": "bad"}):
            self.assertEqual(86400.0, positive_float_env("OMO_MANAGER_SEEN_TTL_S", 86400.0))
        with patch.dict(os.environ, {"OMO_MANAGER_SEEN_TTL_S": "-1"}):
            self.assertEqual(86400.0, positive_float_env("OMO_MANAGER_SEEN_TTL_S", 86400.0))

    def test_pending_watch_does_not_write_seen_state_file(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "seen.tsv"
            path = root / "task.md"
            path.write_text("(pending)\nsource\n", encoding="utf-8")
            with patch("omo_manager.omo_pending_watch.push_ref", return_value=0):
                self.assertEqual(0, watcher.main(["--root", str(root), "--once"]))
            self.assertFalse(state.exists())

    def test_manager_routed_pending_marker_is_not_redelivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            _ = path.write_text("(pending)\n(manager routed: to `task.md`.)\n(from email manager_mail/4480.txt)\n", encoding="utf-8")
            self.assertEqual([], find_markers(root, [path]))

    def test_idle_status_stays_quiet_without_task_state_problem(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "", Path(tmp) / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True)
            out = StringIO()
            with redirect_stdout(out):
                pushed = watcher.maybe_push_idle_status(args, 100.0, 130.0)
            self.assertFalse(pushed)
            self.assertEqual("", out.getvalue())

    def test_idle_status_tick_updates_after_due_check(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True)
        with patch("omo_manager.omo_pending_watch.maybe_push_idle_status", return_value=False) as push:
            self.assertEqual((100.0, None), watcher.update_idle_status_check(args, 100.0, 129.9, None))
            self.assertEqual((130.0, None), watcher.update_idle_status_check(args, 100.0, 130.0, None))
        push.assert_called_once_with(args, 100.0, 130.0)

    def test_idle_status_tick_starts_background_check(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "http://127.0.0.1:1", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, False)
        with patch("omo_manager.omo_pending_watch.start_command", return_value="run") as start:
            self.assertEqual((130.0, "run"), watcher.update_idle_status_check(args, 100.0, 130.0, None))
        start.assert_called_once_with("idle status check", ["/status.py", "--root", "/tmp"], 30)

    def test_status_command_passes_manager_target_to_problem_check(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, "cfg:0.0")
        self.assertEqual(["/status.py", "--root", "/tmp", "--manager-target", "cfg:0.0", "--problems-only"], watcher.status_command(args, True))

    def test_agent_problem_check_uses_compaction_aware_timeout(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True)
        result = subprocess.CompletedProcess(["/status.py"], 0, "", "")
        with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=result) as run:
            self.assertFalse(watcher.maybe_push_agent_problems(args, {}, 1000.0))
        run.assert_called_once()
        self.assertEqual(watcher.DEFAULT_AGENT_PROBLEM_TIMEOUT_S, run.call_args.kwargs["timeout"])

    def test_periodic_status_text_ignores_full_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        result = watcher.CommandOutput("idle status check", 0, "agent-status: running=1\nrunning: task=a.md\n", "")
        self.assertIsNone(watcher.periodic_status_text(Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True), result))

    def test_periodic_status_text_suppresses_manager_self_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        result = watcher.CommandOutput(
            "idle status check",
            0,
            "agent-status: running=1 ready=1\nrunning: task=manager evidence=target=wl:1.0 role=manager output=working\nready: task=worker.md evidence=target=wl:2 output=idle\n",
            "",
        )

        self.assertIsNone(watcher.periodic_status_text(Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1"), result))

    def test_periodic_status_text_returns_none_for_only_manager_self_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        result = watcher.CommandOutput("idle status check", 0, "agent-status: running=1\nrunning: task=manager evidence=target=wl:1.0 role=manager output=working\n", "")

        self.assertIsNone(watcher.periodic_status_text(Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1"), result))

    def test_periodic_status_text_preserves_stale_counts_when_suppressing_manager_self_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        result = watcher.CommandOutput(
            "idle status check",
            0,
            "agent-status: not_codex=0 running=1 blocked_idle=0 error=0 ready=0 stuck_input=0 human_request=0 done-registry-stale=2 pruned=1\nrunning: task=manager evidence=target=wl:1.0 role=manager output=working\n",
            "",
        )

        self.assertIsNone(watcher.periodic_status_text(Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1"), result))

    def test_periodic_status_text_keeps_other_manager_named_task_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        result = watcher.CommandOutput(
            "idle status check",
            0,
            "agent-status: not_codex=0 running=1 blocked_idle=0 error=0 ready=0 stuck_input=0 human_request=0 done-registry-stale=0 pruned=0\nrunning: task=manager.md evidence=target=wl:2.0 role=worker output=working\n",
            "",
        )

        self.assertIsNone(watcher.periodic_status_text(Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1"), result))

    def test_manager_worktree_reminder_from_dirty_output(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        text = watcher.manager_worktree_reminder_from_output(
            "clean: repo=/tmp/other\n"
            "repo=/tmp/work_logs status=M path=work_manager_today.md category=manager-doc-or-task-change\n"
            "repo=/tmp/work_logs status=?? path=new_task.md category=manager-doc-or-task-change\n"
        )

        self.assertIn(
            "omo_pending_watch detected /tmp/work_logs is dirty. Clean it up, let every agent commit their changes. "
            "Commit all task files yourself. Remember NEVER to tell workers about task files.",
            text,
        )
        self.assertIn("work_manager_today.md", text)
        self.assertIn("new_task.md", text)
        self.assertNotIn("status=M", text)
        self.assertNotIn("category=", text)
        self.assertNotIn("clean: repo=/tmp/other", text)

    def test_manager_worktree_reminder_skips_clean_output(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        self.assertEqual("", watcher.manager_worktree_reminder_from_output("clean: repo=/tmp/work_logs\n"))

    def test_manager_worktree_reminder_skips_non_git_repo_error_output(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        text = watcher.manager_worktree_reminder_from_output(
            "repo-error: repo=/tmp error=fatal: not a git repository (or any of the parent directories): .git\n"
        )

        self.assertEqual("", text)

    def test_worktree_result_formats_manager_pwd_cleanliness_reminder(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        result = watcher.CommandOutput(
            "worktree check",
            0,
            "repo=/tmp/work_logs status=M path=work_manager_today.md category=manager-doc-or-task-change\n",
            "",
        )

        text = watcher.worktree_reminder_text_from_result(result, Path("/tmp/work_logs"))

        self.assertIn(
            "omo_pending_watch detected /tmp/work_logs is dirty. Clean it up, let every agent commit their changes. "
            "Commit all task files yourself. Remember NEVER to tell workers about task files.",
            text,
        )
        self.assertIn("work_manager_today.md", text)

    def test_periodic_status_text_reminds_about_non_terminal_task_state(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text(
                "current:\n"
                "pending_task.md cfg:1\n"
                "blocked_task.md cfg:2\n"
                "done_task.md cfg:3\n",
                encoding="utf-8",
            )
            _ = (root / "pending_task.md").write_text(f"{task_frontmatter(runat='cfg:1')}(pending)\n", encoding="utf-8")
            _ = (root / "blocked_task.md").write_text(task_frontmatter(status="blocked", runat="cfg:2", blocked_on="waiting on human"), encoding="utf-8")
            _ = (root / "done_task.md").write_text(task_frontmatter(status="done", runat="cfg:3"), encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1"), result)

            self.assertIsNotNone(text)
            assert text is not None
            self.assertIn("manager task-state reminder: MANAGER.md requires each manager-owned task to have frontmatter", text)
            self.assertIn("task-state: task=pending_task.md status=pending", text)
            self.assertNotIn("blocked_task.md", text)
            self.assertNotIn("done_task.md", text)
            self.assertIn("Single-tag enforcement is intentionally not checked.", text)

    def test_manager_pending_item_reminders_route_to_manager_runat(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text(
                "current:\n"
                "manager_task.md wl:2\n"
                "worker_task.md wl:3\n",
                encoding="utf-8",
            )
            _ = (root / "manager_task.md").write_text(
                task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True, pending_items=("delegate audit work",)),
                encoding="utf-8",
            )
            _ = (root / "worker_task.md").write_text(
                task_frontmatter(runat="wl:3", managerat="wl:1", pending_items=("worker-owned item",)),
                encoding="utf-8",
            )

            reminders = watcher.manager_pending_item_reminder_texts(root)

            self.assertEqual(["wl:2"], sorted(reminders))
            text = reminders["wl:2"]
            self.assertIn("manager pending-item reminder: manager task files should not keep `pending_task_items`.", text)
            self.assertIn("manager-pending-item: task=manager_task.md item=delegate audit work", text)
            self.assertNotIn("worker-owned item", text)

    def test_worker_pending_item_reminders_route_large_lists_to_owner_manager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text(
                "current:\n"
                "small_worker.md wl:3\n"
                "large_worker.md wl:4\n",
                encoding="utf-8",
            )
            _ = (root / "small_worker.md").write_text(
                task_frontmatter(runat="wl:3", managerat="wl:1", pending_items=tuple(f"small {idx}" for idx in range(3))),
                encoding="utf-8",
            )
            _ = (root / "large_worker.md").write_text(
                task_frontmatter(runat="wl:4", managerat="wl:1", pending_items=tuple(f"large {idx}" for idx in range(11))),
                encoding="utf-8",
            )

            reminders = watcher.worker_pending_item_reminder_texts(root)

            self.assertEqual(["wl:1"], sorted(reminders))
            text = reminders["wl:1"]
            self.assertIn("worker pending-item reminder: these task files have large `pending_task_items` lists.", text)
            self.assertIn("worker-pending-items: task=large_worker.md status=running count=11", text)
            self.assertIn("worker-pending-item: task=large_worker.md item=large 0", text)
            self.assertIn("worker-pending-item: task=large_worker.md omitted=8", text)
            self.assertNotIn("small_worker.md", text)

    def test_maybe_push_idle_status_sends_manager_pending_items_to_runat(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nmanager_task.md wl:2\n", encoding="utf-8")
            _ = (root / "manager_task.md").write_text(
                task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True, pending_items=("delegate audit work",)),
                encoding="utf-8",
            )
            out = StringIO()
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1")

            with redirect_stdout(out):
                self.assertTrue(watcher.maybe_push_idle_status(args, 100.0, 131.0))

            text = out.getvalue()
            self.assertIn("manager-pending-item: task=manager_task.md item=delegate audit work", text)
            self.assertNotIn("manager task-state reminder", text)

    def test_maybe_push_idle_status_sends_worker_pending_item_size_reminders(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nlarge_worker.md wl:4\n", encoding="utf-8")
            _ = (root / "large_worker.md").write_text(
                task_frontmatter(runat="wl:4", managerat="wl:1", pending_items=tuple(f"large {idx}" for idx in range(10))),
                encoding="utf-8",
            )
            out = StringIO()
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1")

            with redirect_stdout(out):
                self.assertTrue(watcher.maybe_push_idle_status(args, 100.0, 131.0))

            text = out.getvalue()
            self.assertIn("worker pending-item reminder: these task files have large `pending_task_items` lists.", text)
            self.assertIn("worker-pending-items: task=large_worker.md status=running count=10", text)

    def test_push_manager_pending_item_reminders_keeps_manager_and_worker_reminders(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nmanager_task.md wl:2\nlarge_worker.md wl:4\n", encoding="utf-8")
            _ = (root / "manager_task.md").write_text(
                task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True, pending_items=("delegate audit work",)),
                encoding="utf-8",
            )
            _ = (root / "large_worker.md").write_text(
                task_frontmatter(runat="wl:4", managerat="wl:1", pending_items=tuple(f"large {idx}" for idx in range(10))),
                encoding="utf-8",
            )
            out = StringIO()
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1")

            with redirect_stdout(out):
                self.assertTrue(watcher.push_manager_pending_item_reminders(args))

            text = out.getvalue()
            self.assertIn("manager pending-item reminder: manager task files should not keep `pending_task_items`.", text)
            self.assertIn("manager-pending-item: task=manager_task.md item=delegate audit work", text)
            self.assertIn("worker pending-item reminder: these task files have large `pending_task_items` lists.", text)
            self.assertIn("worker-pending-items: task=large_worker.md status=running count=10", text)

    def test_periodic_status_text_ignores_artifact_paths_in_todo_notes(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text(
                "current:\n"
                "real_task.md wl:1\n"
                "notes: see `docs/vl-proof-standards.md`\n"
                "notes: send `manager_mail/8767_response.md` to vl:2\n"
                "- note: worker output has `ANSWER.md`, `PROCESS.md`, and `TELEMETRY.md`\n"
                "previous:\n"
                "vl_human_followup_proof_standards_8767.md vl:2 (done; documented `docs/vl-proof-standards.md`, response `manager_mail/8767_response.md`)\n"
                "trim_metadata.md vl:83 (done; `ANSWER.md`, `PROCESS.md`, and `TELEMETRY.md` now agree)\n",
                encoding="utf-8",
            )
            _ = (root / "real_task.md").write_text(f"{task_frontmatter(runat='wl:1')}(pending)\n", encoding="utf-8")
            _ = (root / "vl_human_followup_proof_standards_8767.md").write_text(task_frontmatter(status="done", runat="vl:2"), encoding="utf-8")
            _ = (root / "trim_metadata.md").write_text(task_frontmatter(status="done", runat="vl:83"), encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True), result)

            self.assertIsNotNone(text)
            assert text is not None
            self.assertIn("task-state: task=real_task.md status=pending", text)
            self.assertNotIn("docs/vl-proof-standards.md", text)
            self.assertNotIn("manager_mail/8767_response.md", text)
            self.assertNotIn("ANSWER.md", text)
            self.assertNotIn("PROCESS.md", text)
            self.assertNotIn("TELEMETRY.md", text)

    def test_periodic_status_text_reports_real_missing_task_file(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nmissing_task.md wl:1\n", encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True), result)

            self.assertIsNotNone(text)
            assert text is not None
            self.assertIn("task-state: task=missing_task.md status=missing-file", text)

    def test_periodic_status_text_ignores_previous_task_state(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text(
                "previous:\n"
                "old_task.md wl:1 (missing status in old bookkeeping)\n"
                "old_note.md wl:2 (done; mentions `ANSWER.md`)\n",
                encoding="utf-8",
            )
            _ = (root / "old_task.md").write_text("runat: wl:1 codex\n", encoding="utf-8")
            _ = (root / "old_note.md").write_text("runat: wl:2 codex\n(done)\n", encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True), result)

            self.assertIsNone(text)

    def test_periodic_status_text_skips_submanager_owned_task_state_for_main_manager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nvl_worker.md vl:1\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(f"{task_frontmatter(runat='vl:1', managerat='vl:15')}(pending)\n", encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:0"), result)

            self.assertIsNone(text)

    def test_periodic_status_text_skips_submanager_owned_task_state_without_manager_target(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nvl_worker.md vl:1\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(f"{task_frontmatter(runat='vl:1', managerat='vl:15')}(pending)\n", encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True), result)

            self.assertIsNone(text)

    def test_periodic_status_text_skips_missing_vl_task_owned_by_active_submanager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\nvl_worker.md vl:1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:0"), result)

            self.assertIsNone(text)

    def test_periodic_status_text_skips_vl_task_owned_by_bulleted_active_submanager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\n- `vl_submanager_current_8653.md` (`vl:15`)\n- `vl_worker.md` (`vl:1`)\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            result = watcher.CommandOutput("idle status check", 0, "agent-status: running=0\n", "")

            text = watcher.periodic_status_text(Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:0"), result)

            self.assertIsNone(text)

    def test_idle_status_stays_quiet_before_idle_interval(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True)
        out = StringIO()
        with redirect_stdout(out):
            pushed = watcher.maybe_push_idle_status(args, 100.0, 129.9)
        self.assertFalse(pushed)
        self.assertEqual("", out.getvalue())

    def test_agent_problem_check_pushes_only_changed_or_unthrottled_problem(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            status_script = Path(tmp) / "status.py"
            _ = status_script.write_text(
                "#!/usr/bin/env python3\nprint('agent-problems: not_codex=1 error=0 ready=0 done-registry-stale=0')\nprint('not_codex: task=task.md evidence=target=cfg:1')\nraise SystemExit(3)\n",
                encoding="utf-8",
            )
            status_script.chmod(0o700)
            args = Args(Path(tmp), "", Path(tmp) / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True, agent_problem_repeat_s=300.0)
            seen: dict[str, float] = {}
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.maybe_push_agent_problems(args, seen, 1000.0))
                self.assertFalse(watcher.maybe_push_agent_problems(args, seen, 1200.0))
                self.assertTrue(watcher.maybe_push_agent_problems(args, seen, 1300.0))
            text = out.getvalue()
            self.assertEqual(2, text.count("Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:"))
            self.assertNotIn("(from agent omo_pending_watch agent-problem)", text)
            self.assertIn("1 not codex; check if agent failed to launch:", text)
            self.assertIn("task.md cfg:1 <output></output>", text)

    def test_agent_problem_check_reports_stuck_input_after_three_enter_attempts(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=task.md evidence=target=cfg:1 unstick=sent_enter\nunstuck: target=cfg:1 task=task.md action=sent_enter\n",
            "",
        )
        seen: dict[str, float] = {}
        out = StringIO()
        with redirect_stdout(out):
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 1000.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 1001.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1002.0))
        self.assertEqual(1, out.getvalue().count("1 have their input being stuck; unstick or restart them:"))
        self.assertNotIn("(from agent omo_pending_watch agent-problem)", out.getvalue())

    def test_agent_problem_check_pushes_blocked_idle_reports(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: blocked_idle=1\nmanager-action: blocked_idle>0 inspect blocked agents, unblock if possible, or route the exact blocker\nblocked_idle: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl task_status=blocked idle_status=ready reason=image lacks codex\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:", text)
        self.assertIn("1 blocked agents are ready; if they are not actually blocked, correct their status, otherwise make sure whatever is blocking them is being resolved:", text)
        self.assertIn("vl_worker.md vl:9 <blocked_on>image lacks codex</blocked_on>", text)

    def test_agent_problem_check_exponentially_backs_off_blocked_idle_rows(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: blocked_idle=1\nblocked_idle: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl task_status=blocked idle_status=ready reason=image lacks codex\n",
            "",
        )
        seen: dict[str, float] = {}
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1000.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 1599.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1600.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 2499.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 2500.0))
        self.assertEqual(3, out.getvalue().count("1 blocked agents are ready; if they are not actually blocked, correct their status, otherwise make sure whatever is blocking them is being resolved:"))

    def test_agent_problem_check_blocked_idle_backoff_ignores_mixed_report_repeat(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=1800.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "\n".join(
                [
                    "agent-problems: blocked_idle=1 ready=1",
                    "blocked_idle: task=blocked.md evidence=target=cfg:1 task_status=blocked idle_status=ready reason=waiting",
                    "ready: task=ready.md evidence=target=cfg:2 output=idle",
                ]
            ),
            "",
        )
        changed_ready_result = watcher.CommandOutput(
            "agent-problems",
            3,
            "\n".join(
                [
                    "agent-problems: blocked_idle=1 ready=1",
                    "blocked_idle: task=blocked.md evidence=target=cfg:1 task_status=blocked idle_status=ready reason=waiting",
                    "ready: task=ready.md evidence=target=cfg:2 output=changed",
                ]
            ),
            "",
        )
        seen: dict[str, float] = {}
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 10000.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, changed_ready_result, 10599.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, changed_ready_result, 10600.0))
        text = out.getvalue()
        self.assertEqual(2, text.count("blocked.md cfg:1 <blocked_on>waiting</blocked_on>"))
        self.assertEqual(1, text.count("ready.md cfg:2 <output>idle</output>"))
        self.assertEqual(1, text.count("ready.md cfg:2 <output>changed</output>"))

    def test_agent_problem_check_uses_vl_backoff_owner_for_unowned_rows(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=1800.0)
        line = "blocked_idle: task=vl_worker.md evidence=target=vl:1 task_status=blocked idle_status=ready reason=waiting"
        output = f"agent-problems: blocked_idle=1\n{line}"
        seen: dict[str, float] = {}

        self.assertTrue(watcher.agent_problem_output_by_owner(args, seen, output, 10000.0, backoff_owner_target="vl:15"))
        watcher.remember_blocked_idle_report(args, seen, "vl:15", line, 10000.0)
        self.assertEqual({}, watcher.agent_problem_output_by_owner(args, seen, output, 10599.0, backoff_owner_target="vl:15"))
        self.assertTrue(watcher.agent_problem_output_by_owner(args, seen, output, 10600.0, backoff_owner_target="vl:15"))

    def test_agent_problem_check_dispatches_rows_to_owner_targets(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:16", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "\n".join(
                [
                    "agent-problems: blocked_idle=1 ready=1",
                    "manager-action: blocked_idle>0 inspect blocked agents, unblock if possible, or route the exact blocker",
                    "blocked_idle: task=owned.md evidence=target=cfg:1 task_status=blocked idle_status=ready reason=waiting owner_target=wl:17",
                    "ready: task=local.md evidence=target=wl:3 output=idle",
                ]
            ),
            "",
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(capture_delivery_call(command))
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        self.assertEqual(2, len(calls))
        by_target = {call[call.index("--manager-target") + 1]: call[1] for call in calls}
        self.assertIn("wl:17", by_target)
        self.assertIn("wl:16", by_target)
        self.assertIn("owned.md cfg:1 <blocked_on>waiting</blocked_on>", by_target["wl:17"])
        self.assertNotIn("local.md", by_target["wl:17"])
        self.assertIn("local.md wl:3 <output>idle</output>", by_target["wl:16"])
        self.assertNotIn("owned.md", by_target["wl:16"])
        self.assertNotIn("blocked agents are ready", by_target["wl:16"])

    def test_agent_problem_check_formats_untracked_agent_group(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:16", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: untracked_agent=1\nuntracked_agent: task=tmux:vl:11 evidence=target=vl:11 role=tmux_unmanaged output=Implemented and privately reported owner_target=vl:15\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("1 not tracked in any task file; ask them what their task is, or consider resuming or closing them:", text)
        self.assertIn("vl:11 <output>Implemented and privately reported</output>", text)
        self.assertNotIn("role=tmux_unmanaged", text)

    def test_agent_problem_check_dispatches_unstuck_to_row_owner(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:16", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "\n".join(
                [
                    "agent-problems: stuck_input=1",
                    "stuck_input: task=owned.md evidence=target=cfg:1 task_status=running unstick=sent_enter owner_target=wl:17",
                    "unstuck: target=cfg:1 task=owned.md action=sent_enter",
                ]
            ),
            "",
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(capture_delivery_call(command))
            return subprocess.CompletedProcess(command, 0)

        seen: dict[str, float] = {}
        with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 1000.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 1001.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1002.0))
        self.assertEqual(1, len(calls))
        self.assertEqual("wl:17", calls[0][calls[0].index("--manager-target") + 1])
        self.assertIn("owned.md cfg:1", calls[0][1])

    def test_agent_problem_check_does_not_treat_same_target_worker_as_manager_self(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:16", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "\n".join(
                [
                    "agent-problems: error=1 stuck_input=1",
                    "error: task=owned.md evidence=target=wl:16 output=worker error owner_target=vl:15",
                    "stuck_input: task=other.md evidence=target=wl:16 task_status=running input=queued prompt unstick=not_safe:plan_prompt owner_target=vl:15",
                ]
            ),
            "",
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(capture_delivery_call(command))
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run), patch.object(watcher, "email_human_manager_problem", side_effect=AssertionError("unexpected human email")):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        self.assertEqual(1, len(calls))
        self.assertEqual("vl:15", calls[0][calls[0].index("--manager-target") + 1])
        self.assertIn("owned.md wl:16 <output>worker error</output>", calls[0][1])
        self.assertIn("other.md wl:16 <input>queued prompt</input>", calls[0][1])

    def test_agent_problem_check_clears_enter_attempts_after_target_is_no_longer_stuck(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:16", agent_problem_repeat_s=300.0)
        stuck = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=owned.md evidence=target=cfg:1 task_status=running unstick=sent_enter owner_target=wl:17\n",
            "",
        )
        ready = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: ready=1\nready: task=other.md evidence=target=cfg:2 output=idle owner_target=wl:17\n",
            "",
        )
        seen: dict[str, float] = {}
        with redirect_stdout(StringIO()):
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, stuck, 1000.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, stuck, 1001.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, ready, 1002.0))
        self.assertFalse(any(key.startswith("agent-problem-enter-attempt:/tmp:cfg:1:") for key in seen))

    def test_agent_problem_check_keeps_enter_attempts_while_target_is_still_stuck(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:16", agent_problem_repeat_s=300.0)
        sent_enter = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=owned.md evidence=target=cfg:1 task_status=running unstick=sent_enter owner_target=wl:17\n",
            "",
        )
        still_stuck = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=owned.md evidence=target=cfg:1 task_status=running unstick=not_safe:plan_prompt owner_target=wl:17\n",
            "",
        )
        seen: dict[str, float] = {}
        with redirect_stdout(StringIO()):
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, sent_enter, 1000.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, sent_enter, 1001.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, still_stuck, 1002.0))
        self.assertEqual(2, sum(1 for key in seen if key.startswith("agent-problem-enter-attempt:/tmp:cfg:1:")))

    def test_agent_problem_check_clears_enter_attempts_after_clean_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:16", agent_problem_repeat_s=300.0)
        seen = {
            "agent-problem-enter-attempt:/tmp:cfg:1:1": 1000.0,
            "agent-problem-enter-attempt:/tmp:cfg:1:2": 1001.0,
        }
        result = watcher.CommandOutput("agent-problems", 0, "", "")
        self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1002.0))
        self.assertEqual({}, seen)

    def test_filter_manager_self_problem_output_drops_human_request(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        output = "\n".join(
            [
                "agent-problems: stuck_input=1 human_request=1",
                "stuck_input: task=manager evidence=target=wl:1 role=manager unstick=not_safe:plan_prompt",
                "human_request: task=owned.md evidence=pending_item=close stale worker owner_target=wl:17",
            ]
        )

        text = watcher.filter_manager_self_problem_output(output, "wl:1")

        self.assertIsNone(text)

    def test_filter_manager_compaction_output_drops_human_request(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        output = "\n".join(
            [
                "agent-problems: manager_compaction=1 human_request=1",
                "manager_compaction: task=manager evidence=target=wl:1 role=manager output=Compacting",
                "human_request: task=owned.md evidence=pending_item=close stale worker owner_target=wl:17",
            ]
        )

        text = watcher.filter_manager_compaction_output(output, "wl:1")

        self.assertIsNone(text)

    def test_agent_problem_check_routes_owner_tagged_rows_to_their_manager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_script = root / "status.py"
            _ = status_script.write_text(
                "#!/usr/bin/env python3\n"
                "print('agent-problems: blocked_idle=1 ready=1 human_request=1')\n"
                "print('manager-action: blocked_idle>0 inspect blocked agents')\n"
                "print('blocked_idle: task=vl_worker.md evidence=target=vl:1 role=blocked_idle_vl task_status=blocked idle_status=ready reason=waiting owner_target=vl:15')\n"
                "print('ready: task=vl_owned.md evidence=target=vl:2 output=owned owner_target=vl:15')\n"
                "print('human_request: task=vl_done.md evidence=pending_item=clear human-facing terms owner_target=vl:15')\n"
                "raise SystemExit(3)\n",
                encoding="utf-8",
            )
            status_script.chmod(0o700)
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\nvl_worker.md vl:1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, False, manager_target="wl:16.0", agent_problem_repeat_s=300.0)
            calls: list[list[str]] = []
            real_run = subprocess.run

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if command and command[0] == str(status_script):
                    return real_run(command, **kwargs)
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.maybe_push_agent_problems(args, {}, 1000.0))
            self.assertEqual("vl:15", calls[0][calls[0].index("--manager-target") + 1])
            pushed_text = calls[0][1]
            self.assertIn("vl_worker.md vl:1 <blocked_on>waiting</blocked_on>", pushed_text)
            self.assertIn("vl_owned.md vl:2 <output>owned</output>", pushed_text)
            self.assertNotIn("vl_done.md", pushed_text)

    def test_agent_problem_check_routes_rows_to_owner_despite_manager_target(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_script = root / "status.py"
            _ = status_script.write_text(
                "#!/usr/bin/env python3\n"
                "print('agent-problems: blocked_idle=1')\n"
                "print('blocked_idle: task=vl_worker.md evidence=target=vl:1 task_status=blocked idle_status=ready reason=waiting owner_target=vl:15')\n"
                "raise SystemExit(3)\n",
                encoding="utf-8",
            )
            status_script.chmod(0o700)
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\nvl_worker.md vl:1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter(runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, False, manager_target="wl:16.0", agent_problem_repeat_s=300.0)
            calls: list[list[str]] = []
            real_run = subprocess.run

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if command and command[0] == str(status_script):
                    return real_run(command, **kwargs)
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run):
                self.assertTrue(watcher.maybe_push_agent_problems(args, {}, 1000.0))
            self.assertEqual(1, len(calls))
            self.assertEqual("vl:15", calls[0][calls[0].index("--manager-target") + 1])
            self.assertIn("vl_worker.md vl:1 <blocked_on>waiting</blocked_on>", calls[0][1])

    def test_agent_problem_check_can_add_manager_policy_reminder(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(
            Path("/tmp"),
            "",
            Path("/tmp/seen.tsv"),
            1.0,
            1.0,
            30.0,
            Path("/status.py"),
            False,
            True,
            agent_problem_repeat_s=300.0,
            reminder_random=lambda: 0.0,
            reminder_choice=lambda reminders: reminders[0],
        )
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: blocked_idle=1\nblocked_idle: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl task_status=blocked idle_status=ready reason=image lacks codex\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:", text)
        self.assertIn("Reminder: delegate work; do not do worker work in the manager.", text)

    def test_agent_problem_check_routes_manager_self_problem_to_active_manager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrescue_manager.md wl:2\nsame_manager.md wl:1\nworker.md wl:3\n", encoding="utf-8")
            _ = (root / "rescue_manager.md").write_text(task_frontmatter(runat="wl:2", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            _ = (root / "same_manager.md").write_text(task_frontmatter(runat="wl:1", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter(runat="wl:3", managerat="wl:1.0"), encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:1.0", agent_problem_repeat_s=300.0, reminder_choice=lambda targets: targets[0])
            result = watcher.CommandOutput(
                "agent-problems",
                3,
                "agent-problems: error=1\nerror: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n",
                "",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run), patch.object(watcher, "email_human_manager_problem", side_effect=AssertionError("unexpected human email")):
                self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
            self.assertEqual(1, len(calls))
            self.assertEqual("wl:2", calls[0][calls[0].index("--manager-target") + 1])
            pushed_text = calls[0][1]
            self.assertIn("Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:", pushed_text)
            self.assertIn("manager (this is the main manager) wl:1.0 <output>Selected model is at capacity</output>", pushed_text)
            self.assertNotIn("worker.md", pushed_text)

    def test_agent_problem_check_can_route_manager_self_problem_to_running_low_priority_manager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nsame_manager.md wl:1\nlow priority:\nrescue_manager.md wl:2\n", encoding="utf-8")
            _ = (root / "same_manager.md").write_text(task_frontmatter(runat="wl:1", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            _ = (root / "rescue_manager.md").write_text(task_frontmatter(runat="wl:2", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:1.0", agent_problem_repeat_s=300.0, reminder_choice=lambda targets: targets[0])
            result = watcher.CommandOutput(
                "agent-problems",
                3,
                "agent-problems: error=1\nerror: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n",
                "",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run), patch.object(watcher, "email_human_manager_problem", side_effect=AssertionError("unexpected human email")):
                self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
            self.assertEqual(1, len(calls))
            self.assertEqual("wl:2", calls[0][calls[0].index("--manager-target") + 1])

    def test_agent_problem_check_does_not_block_on_peer_manager_delivery_result(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nfirst_manager.md wl:2\nsecond_manager.md wl:3\n", encoding="utf-8")
            _ = (root / "first_manager.md").write_text(task_frontmatter(runat="wl:2", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            _ = (root / "second_manager.md").write_text(task_frontmatter(runat="wl:3", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:1.0", agent_problem_repeat_s=300.0, reminder_choice=lambda targets: targets[0])
            result = watcher.CommandOutput(
                "agent-problems",
                3,
                "agent-problems: error=1\nerror: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n",
                "",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                target = command[command.index("--target") + 1]
                return subprocess.CompletedProcess(command, 2 if target == "wl:2" else 0)

            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run), patch.object(watcher, "email_human_manager_problem", side_effect=AssertionError("unexpected human email")):
                self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
            self.assertEqual(["wl:2"], [call[call.index("--manager-target") + 1] for call in calls])

    def test_agent_problem_check_keeps_worker_owner_routing_with_manager_self_problem(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrescue_manager.md wl:2\nowner_manager.md wl:3\n", encoding="utf-8")
            _ = (root / "rescue_manager.md").write_text(task_frontmatter(runat="wl:2", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            _ = (root / "owner_manager.md").write_text(task_frontmatter(runat="wl:3", managerat="wl:1.0", is_manager=True), encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:1.0", agent_problem_repeat_s=300.0, reminder_choice=lambda targets: targets[0])
            result = watcher.CommandOutput(
                "agent-problems",
                3,
                "agent-problems: error=2\n"
                "error: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n"
                "error: task=worker.md evidence=target=wl:4 output=worker failed owner_target=wl:3\n",
                "",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(capture_delivery_call(command))
                return subprocess.CompletedProcess(command, 0)

            with patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run), patch.object(watcher, "email_human_manager_problem", side_effect=AssertionError("unexpected human email")):
                self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
            self.assertEqual(["wl:2", "wl:3"], [call[call.index("--manager-target") + 1] for call in calls])
            self.assertIn("manager (this is the main manager) wl:1.0 <output>Selected model is at capacity</output>", calls[0][1])
            self.assertIn("worker.md wl:4 <output>worker failed</output>", calls[1][1])

    def test_agent_problem_check_emails_human_for_manager_self_stuck_prompt(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=manager evidence=target=wl:1.0 role=manager output=Create a plan? shift + tab use Plan mode esc dismiss unstick=not_safe:plan_prompt\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("manager human email due: manager watcher detected manager error", text)
        self.assertIn("agent-problems: stuck_input=1", text)
        self.assertIn("stuck_input: task=manager evidence=target=wl:1.0 role=manager", text)
        self.assertIn("not_safe:plan_prompt", text)
        self.assertIn("suppressed manager self-problem report", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)

    def test_agent_problem_check_suppresses_resolved_manager_self_stuck_prompt(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=manager evidence=target=wl:1.0 role=manager output=Create a plan? shift + tab use Plan mode esc dismiss unstick=not_stuck\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertFalse(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("suppressed manager self-problem report", text)
        self.assertNotIn("manager human email due: manager watcher detected manager error", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)

    def test_agent_problem_check_routes_worker_alias_prompt_to_manager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=active.md evidence=target=wl:1 output=Create a plan? shift + tab use Plan mode esc dismiss unstick=not_safe:plan_prompt\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:", text)
        self.assertIn("active.md wl:1 <input>Create a plan? shift + tab use Plan mode esc dismiss</input>", text)
        self.assertNotIn("manager human email due: manager watcher detected manager error", text)
        self.assertNotIn("suppressed manager self-problem report", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)

    def test_agent_problem_check_reminds_manager_after_compaction_once(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: manager_compaction=1\nmanager-action: manager_compaction>0 reread MANAGER.md after compaction unless the compaction summary already included it\nmanager_compaction: task=manager evidence=target=wl:1.0 role=manager output=• Compacting conversation / › Continue managing\n",
            "",
        )
        seen: dict[str, float] = {}
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1000.0))
            self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 1001.0))
        text = out.getvalue()
        self.assertEqual(1, text.count("Unless you know the exact content of MANAGER.md, read it. Normally, don't ack human"))
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)

    def test_agent_problem_check_clears_manager_compaction_active_when_gone(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        seen = {watcher.manager_compaction_active_key(args): 1000.0}
        result = watcher.CommandOutput("agent-problems", 0, "", "")
        self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1010.0))
        self.assertNotIn(watcher.manager_compaction_active_key(args), seen)

    def test_agent_problem_check_clears_manager_compaction_active_when_other_problem_remains(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        seen = {watcher.manager_compaction_active_key(args): 1000.0}
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: ready=1\nready: task=active.md evidence=target=cfg:1.0 output=› Use /skills to list available skills\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1010.0))
        self.assertNotIn(watcher.manager_compaction_active_key(args), seen)
        self.assertIn("Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:", out.getvalue())

    def test_agent_problem_check_emails_human_for_manager_error(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: error=1\nerror: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("manager human email due: manager watcher detected manager error", text)
        self.assertIn("agent-problems: error=1", text)
        self.assertIn("error: task=manager evidence=target=wl:1.0 role=manager", text)
        self.assertIn("suppressed manager self-problem report", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)

    def test_agent_problem_check_invokes_human_email_helper_for_manager_error(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = root / "fake-email.sh"
            helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, root / "status.py", False, False, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
            result = watcher.CommandOutput(
                "agent-problems",
                3,
                "agent-problems: error=1\nerror: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n",
                "",
            )
            launched: dict[str, object] = {}

            def fake_popen(command: list[str], **kwargs: object) -> object:
                launched["command"] = command
                launched["kwargs"] = kwargs
                launched["subject"] = Path(command[command.index("--subject-file") + 1]).read_text(encoding="utf-8")
                launched["body"] = Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8")

                class Process:
                    pid = 4002

                return Process()

            with patch.object(watcher, "DEFAULT_HUMAN_EMAIL_HELPER", helper), patch("omo_manager.omo_pending_watch.subprocess.Popen", side_effect=fake_popen):
                self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
            command = launched["command"]
            self.assertIsInstance(command, list)
            assert isinstance(command, list)
            self.assertEqual(sys.executable, command[0])
            self.assertEqual("-c", command[1])
            self.assertIn(str(helper), command)
            self.assertIn("--manager-human", command)
            self.assertEqual("wl:1.0", command[command.index("--sender-tmux-target") + 1])
            self.assertEqual("manager watcher detected manager error\n", launched["subject"])
            body = launched["body"]
            self.assertIsInstance(body, str)
            assert isinstance(body, str)
            self.assertIn("The manager watcher detected a manager pane problem", body)
            self.assertIn("agent-problems: error=1", body)
            self.assertIn("error: task=manager evidence=target=wl:1.0 role=manager", body)

    def test_agent_problem_check_suppresses_worker_alias_sent_enter_until_retry_limit(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=active.md evidence=target=wl:1 unstick=sent_enter\nunstuck: target=wl:1 task=active.md action=sent_enter\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertFalse(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertEqual("", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)
        self.assertNotIn("unstuck:", text)

    def test_markdown_inotify_watcher_reports_new_file(self) -> None:
        from omo_manager.omo_pending_watch import MarkdownChangeWatcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watcher = MarkdownChangeWatcher.open(root)
            if watcher is None:
                self.skipTest("inotify unavailable")
            try:
                path = root / "task.md"
                path.write_text("(pending)\nnew event\n", encoding="utf-8")
                files, full_scan, notified = watcher.wait(2.0)
            finally:
                watcher.close()
            self.assertTrue(notified)
            self.assertFalse(full_scan)
            self.assertIn(path, files)

    def test_manager_mail_inotify_event_forces_full_scan(self) -> None:
        from omo_manager.omo_pending_watch import MarkdownChangeWatcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            watcher = MarkdownChangeWatcher.open(root)
            if watcher is None:
                self.skipTest("inotify unavailable")
            try:
                (mail_dir / "4002.txt").write_text("Subject: ready\n\nbody\n", encoding="utf-8")
                files, full_scan, notified = watcher.wait(2.0)
            finally:
                watcher.close()
            self.assertTrue(notified)
            self.assertTrue(full_scan)
            self.assertEqual([], files)

    def test_pending_watch_poll_backstop_interval_is_configurable(self) -> None:
        from omo_manager.omo_pending_watch import parse_args

        args = parse_args(["--root", "/tmp/root", "--poll-backstop-interval-s", "12.5", "--once"])
        self.assertEqual(12.5, args.poll_backstop_interval_s)

    def test_pending_watch_poll_backstop_detects_silent_missed_event(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("old\n", encoding="utf-8")
            state = watcher.FileState(mtimes_ns={})
            self.assertEqual([path], watcher.mtime_changed_markdown_files(root, state))
            self.assertEqual([], watcher.mtime_changed_markdown_files(root, state))
            path.write_text("(pending)\nmissed by inotify\n", encoding="utf-8")
            os.utime(path, ns=(2_000_000_000, 2_000_000_000))
            changed = watcher.mtime_changed_markdown_files(root, state)
            self.assertEqual([path], changed)
            args = Args(root, "", root / "seen.tsv", 1.0, 300.0, 30.0, Path("/missing-status.py"), False, True)
            seen: dict[str, float] = {}
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, seen, changed))
            self.assertIn("<snippet file=\"task.md:1-2\">", out.getvalue())

    def test_pending_watch_reminds_when_todo_exceeds_200_lines(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = root / "TODO.md"
            todo.write_text("\n".join(f"line {idx}" for idx in range(201)) + "\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True)
            seen: dict[str, float] = {}
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, seen, [todo]))
            text = out.getvalue()
            self.assertIn("omo_pending_watch detected TODO.md with 201 lines is too long.", text)
            self.assertIn("Move done material to YYYYMM/old_todos.md", text)
            todo.write_text(todo.read_text(encoding="utf-8") + "another line\n", encoding="utf-8")
            out = StringIO()
            with redirect_stdout(out):
                self.assertFalse(watcher.scan_once(args, seen, [todo]))
            self.assertEqual("", out.getvalue())
            todo.write_text("\n".join(f"line {idx}" for idx in range(200)) + "\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertTrue(watcher.scan_once(args, seen, [todo]))
            todo.write_text(todo.read_text(encoding="utf-8") + "over threshold\n", encoding="utf-8")
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, seen, [todo]))
            text = out.getvalue()
            self.assertIn("omo_pending_watch detected TODO.md with 201 lines is too long.", text)

    def test_pending_delivery_launch_failure_is_retryable(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, False, manager_target="wl:1")
            seen: dict[str, float] = {}
            err = StringIO()
            with patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=OSError("exec failed")), redirect_stderr(err):
                self.assertFalse(watcher.scan_once(args, seen, [path]))
            self.assertEqual({}, seen)
            self.assertIn("pending delivery failed: exec failed", err.getvalue())

    def test_pending_delivery_async_launch_does_not_mark_seen(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, False, manager_target="wl:1")
            seen: dict[str, float] = {}
            with patch("omo_manager.omo_pending_watch.send_to_codex", return_value=object()):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertEqual({}, seen)

    def test_delivery_success_event_records_seen_key_on_watcher_thread(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True)
        future: Future[None] = Future()
        future.set_result(None)
        watcher.log_send_result(future, watcher.DeliverySuccessEvent(seen_keys=("delivery-key",), seen_at_s=1000.0))
        seen: dict[str, float] = {}

        self.assertTrue(watcher.drain_delivery_successes(args, seen, 1001.0))

        self.assertIn("delivery-key", seen)

    def test_manager_delivery_to_owner_sets_async_main_fallback(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/missing-status.py"), False, False, manager_target="wl:1")
        event = watcher.DeliverySuccessEvent(seen_keys=("agent-problem:abc",), seen_at_s=1000.0)
        future: Future[None] = Future()
        future.set_result(None)
        captured: dict[str, object] = {}

        def fake_send_to_codex(
            target: str,
            message: str,
            options: watcher.CodexSendOptions,
            *,
            pending_guard: watcher.PendingGuard | None = None,
            success_event: watcher.DeliverySuccessEvent | None = None,
            failure_fallback: watcher.DeliveryFailureFallback | None = None,
        ) -> Future[None]:
            captured["target"] = target
            captured["message"] = message
            captured["pending_guard"] = pending_guard
            captured["success_event"] = success_event
            captured["failure_fallback"] = failure_fallback
            return future

        with patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=fake_send_to_codex):
            self.assertEqual(watcher.ASYNC_DELIVERY_STARTED, watcher.push_manager_text_to_target(args, "body", "vl:64", event))
        fallback = captured["failure_fallback"]
        self.assertIsInstance(fallback, watcher.DeliveryFailureFallback)
        assert isinstance(fallback, watcher.DeliveryFailureFallback)
        self.assertEqual("vl:64", fallback.failed_target)
        self.assertEqual("wl:1", fallback.target)
        self.assertEqual("body", fallback.text)
        self.assertIs(event, fallback.success_event)

    def test_async_manager_delivery_unavailable_falls_back_to_main_manager(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        future: Future[None] = Future()
        future.set_exception(RuntimeError("target is not a Codex pane after submit: vl:64"))
        event = watcher.DeliverySuccessEvent(seen_keys=("agent-problem:abc",), seen_at_s=1000.0)
        fallback = watcher.DeliveryFailureFallback(
            "vl:64",
            "wl:1",
            "Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:\n\n1 ready and not blocked; consider resuming or closing them:\nworker.md vl:2 <output>idle</output>",
            watcher.CodexSendOptions(2, 0.15, False),
            event,
        )
        calls: list[tuple[str, str, watcher.DeliverySuccessEvent | None]] = []
        fallback_future: Future[None] = Future()
        fallback_future.set_result(None)

        def fake_submit(
            target: str,
            message: str,
            _options: watcher.CodexSendOptions,
            pending_guard: watcher.PendingGuard | None = None,
            success_event: watcher.DeliverySuccessEvent | None = None,
            failure_fallback: watcher.DeliveryFailureFallback | None = None,
        ) -> Future[None]:
            self.assertIsNone(pending_guard)
            self.assertIsNone(failure_fallback)
            calls.append((target, message, success_event))
            return fallback_future

        err = StringIO()
        with patch("omo_manager.omo_pending_watch.submit_send", side_effect=fake_submit), redirect_stderr(err):
            watcher.log_send_result(future, failure_fallback=fallback)
        self.assertEqual(1, len(calls))
        self.assertEqual("wl:1", calls[0][0])
        self.assertIn("Delivery to resolved target `vl:64` failed:", calls[0][1])
        self.assertIn("worker.md vl:2 <output>idle</output>", calls[0][1])
        self.assertIs(event, calls[0][2])
        self.assertIn("async delivery failed: target is not a Codex pane after submit: vl:64", err.getvalue())

    def test_agent_problem_owner_async_failure_falls_back_and_records_seen_after_main_success(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, False, manager_target="wl:1", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "\n".join(
                [
                    "agent-problems: ready=1",
                    "ready: task=worker.md evidence=target=vl:2 task_status=running output=idle owner_target=vl:64",
                ]
            ),
            "",
        )
        seen: dict[str, float] = {}
        owner_future: Future[None] = Future()
        main_future: Future[None] = Future()
        submitted: list[tuple[str, str, watcher.DeliverySuccessEvent | None, watcher.DeliveryFailureFallback | None]] = []

        def fake_send_to_codex(
            target: str,
            message: str,
            _options: watcher.CodexSendOptions,
            *,
            pending_guard: watcher.PendingGuard | None = None,
            success_event: watcher.DeliverySuccessEvent | None = None,
            failure_fallback: watcher.DeliveryFailureFallback | None = None,
        ) -> Future[None]:
            self.assertIsNone(pending_guard)
            submitted.append((target, message, success_event, failure_fallback))
            self.assertEqual("vl:64", target)
            return owner_future

        def fake_submit(
            target: str,
            message: str,
            _options: watcher.CodexSendOptions,
            pending_guard: watcher.PendingGuard | None = None,
            success_event: watcher.DeliverySuccessEvent | None = None,
            failure_fallback: watcher.DeliveryFailureFallback | None = None,
        ) -> Future[None]:
            self.assertIsNone(pending_guard)
            self.assertIsNone(failure_fallback)
            submitted.append((target, message, success_event, failure_fallback))
            self.assertEqual("wl:1", target)
            return main_future

        with patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=fake_send_to_codex), patch("omo_manager.omo_pending_watch.submit_send", side_effect=fake_submit):
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1000.0))
            self.assertEqual({}, seen)
            owner_future.set_exception(RuntimeError("target is not a Codex pane after submit: vl:64"))
            watcher.log_send_result(owner_future, submitted[0][2], submitted[0][3])
            self.assertEqual({}, seen)
            self.assertEqual(2, len(submitted))
            self.assertEqual("wl:1", submitted[1][0])
            self.assertIn("Delivery to resolved target `vl:64` failed:", submitted[1][1])
            self.assertIn("worker.md vl:2 <output>idle</output>", submitted[1][1])
            main_future.set_result(None)
            watcher.log_send_result(main_future, submitted[1][2])
            self.assertTrue(watcher.drain_delivery_successes(args, seen, 1001.0))
        self.assertTrue(any(key.startswith("agent-problem:") for key in seen))

    def test_manager_delivery_launch_failure_is_retryable(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = root / "TODO.md"
            todo.write_text("\n".join(f"line {idx}" for idx in range(201)) + "\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, False, manager_target="wl:1")
            seen: dict[str, float] = {}
            err = StringIO()
            with patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=OSError("exec failed")), redirect_stderr(err):
                self.assertFalse(watcher.scan_once(args, seen, [todo]))
            self.assertEqual({}, seen)
            self.assertIn("manager delivery failed: exec failed", err.getvalue())

    def test_cli_emails_human_when_watcher_crashes(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        sent: dict[str, str] = {}

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            sent["helper"] = command[0]
            sent["sender_target"] = command[command.index("--sender-tmux-target") + 1]
            sent["subject"] = Path(command[command.index("--subject-file") + 1]).read_text(encoding="utf-8")
            sent["body"] = Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(watcher, "main", side_effect=RuntimeError("boom")),
            patch.object(watcher, "DEFAULT_HUMAN_EMAIL_HELPER", Path("/fake/email_me.py")),
            patch.object(watcher, "DEFAULT_MANAGER_TARGET", "wl:1"),
            patch("omo_manager.omo_pending_watch.subprocess.run", side_effect=fake_run),
            self.assertRaises(RuntimeError),
        ):
            watcher.cli(["--root", "/tmp/work_logs"])
        self.assertEqual("/fake/email_me.py", sent["helper"])
        self.assertEqual("wl:1", sent["sender_target"])
        self.assertEqual("pending watcher crashed\n", sent["subject"])
        self.assertIn("The pending watcher crashed unexpectedly.", sent["body"])
        self.assertIn("RuntimeError: boom", sent["body"])

    def test_background_agent_problem_check_does_not_block_pending_scan(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_script = root / "slow-status.sh"
            status_script.write_text(
                "#!/usr/bin/env bash\nsleep 0.5\nprintf 'agent-problems: not_codex=1 error=0 ready=0 done-registry-stale=0\\n'\nexit 3\n",
                encoding="utf-8",
            )
            status_script.chmod(0o700)
            run = watcher.start_command("agent problem check", [str(status_script)], 30)
            self.assertIsNotNone(run)
            assert run is not None
            self.assertIsNone(watcher.poll_command(run, time.time()))
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True)
            seen: dict[str, float] = {}
            out = StringIO()
            started_s = time.monotonic()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertLess(time.monotonic() - started_s, 0.2)
            self.assertIn("<snippet file=\"task.md:1-2\">", out.getvalue())
            while True:
                result = watcher.poll_command(run, time.time())
                if result is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(3, result.returncode)

    def test_agent_problem_check_stays_quiet_on_healthy_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            status_script = Path(tmp) / "status.py"
            _ = status_script.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
            status_script.chmod(0o700)
            args = Args(Path(tmp), "", Path(tmp) / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True)
            out = StringIO()
            with redirect_stdout(out):
                self.assertFalse(watcher.maybe_push_agent_problems(args, {}, 1000.0))
            self.assertEqual("", out.getvalue())

    def test_idle_digest_delivery_waits_for_one_hour_without_human_email(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            mail = mail_dir / "5125.txt"
            _ = mail.write_text("Subject: test\n", encoding="utf-8")
            os.utime(mail, (1000.0, 1000.0))
            _ = (root / "manager_digest.md").write_text("digest item\n", encoding="utf-8")
            args = Args(
                root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True, mail_dir=mail_dir, digest_script=root / "scripts" / "manager-digest", digest_idle_after_s=3600.0
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertFalse(watcher.maybe_deliver_idle_digest(args, 0.0, 4599.9))
                self.assertTrue(watcher.maybe_deliver_idle_digest(args, 0.0, 4600.0))
            self.assertIn("no human email for 3600s", out.getvalue())

    def test_idle_digest_delivery_runs_manager_digest_script(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            mail = mail_dir / "5125.txt"
            _ = mail.write_text("Subject: old\n", encoding="utf-8")
            os.utime(mail, (1000.0, 1000.0))
            _ = (root / "manager_digest.md").write_text("digest item\n", encoding="utf-8")
            script = root / "manager-digest"
            log = root / "deliver.log"
            _ = script.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$PWD $*\" > {log}\n", encoding="utf-8")
            script.chmod(0o700)
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, False, mail_dir=mail_dir, digest_script=script, digest_idle_after_s=3600.0)
            self.assertTrue(watcher.maybe_deliver_idle_digest(args, 0.0, 4600.0))
            self.assertEqual(f"{root} deliver\n", log.read_text(encoding="utf-8"))

    def test_omo_dispatch_strips_pending_and_direct_markers_then_clears_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            capture = tmp_path / "captured-prompt.txt"
            tmux_send = bin_dir / "omo_tmux_send.py"
            tmux_send.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
message_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --message-file) message_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
prompt_file=$(sed -n 's/^Read the dispatch prompt from \\(.*\\) and follow it exactly\\.$/\\1/p' "$message_file")
cp "$prompt_file" "$OMO_CAPTURE_PROMPT"
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)
            task = root / "task.md"
            task.write_text("header\n  (pending)  \nDM only. Send this to the worker.\n", encoding="utf-8")

            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "2",
                    "--end",
                    "3",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home), "OMO_CAPTURE_PROMPT": str(capture)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual("Send this to the worker.", capture.read_text(encoding="utf-8"))
            task_text = task.read_text(encoding="utf-8")
            self.assertNotIn("(pending)", task_text)
            self.assertIn("(manager dispatch:", task_text)

    def test_omo_dispatch_strips_marker_line_without_corrupting_following_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            capture = tmp_path / "captured-prompt.txt"
            tmux_send = bin_dir / "omo_tmux_send.py"
            tmux_send.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
message_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --message-file) message_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
prompt_file=$(sed -n 's/^Read the dispatch prompt from \\(.*\\) and follow it exactly\\.$/\\1/p' "$message_file")
cp "$prompt_file" "$OMO_CAPTURE_PROMPT"
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)
            task = root / "task.md"
            task.write_text("header\n(pending)\nDM\n> quote\n- item\n(for manager: ask agent to report back to manager)\n", encoding="utf-8")

            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "2",
                    "--end",
                    "6",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home), "OMO_CAPTURE_PROMPT": str(capture)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            prompt = capture.read_text(encoding="utf-8")
            self.assertIn("> quote", prompt)
            self.assertIn("- item", prompt)
            self.assertNotIn("(for manager:", prompt)
            self.assertIn("REPORT_FILE=$(omo_report.sh", prompt)
            self.assertIn('omo_report.sh --status STATUS --message-file "$REPORT_FILE"', prompt)
            self.assertNotIn("--agent agent-name", prompt)
            self.assertNotIn("--task-file", prompt)
            self.assertNotIn("--root", prompt)
            self.assertIn("Do not use cat, heredocs, or shell text injection for report bodies.", prompt)

    def test_omo_dispatch_preserves_same_line_payload_syntax_after_dm_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            capture = tmp_path / "captured-prompt.txt"
            tmux_send = bin_dir / "omo_tmux_send.py"
            tmux_send.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
message_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --message-file) message_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
prompt_file=$(sed -n 's/^Read the dispatch prompt from \\(.*\\) and follow it exactly\\.$/\\1/p' "$message_file")
cp "$prompt_file" "$OMO_CAPTURE_PROMPT"
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)
            task = root / "task.md"
            task.write_text("header\n(pending)\nDM: - [ ] fix task\n> quoted payload\n", encoding="utf-8")

            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "2",
                    "--end",
                    "4",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home), "OMO_CAPTURE_PROMPT": str(capture)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual("- [ ] fix task\n> quoted payload", capture.read_text(encoding="utf-8"))

    def test_omo_dispatch_preserves_quoted_trailing_dm_marker_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            capture = tmp_path / "captured-prompt.txt"
            tmux_send = bin_dir / "omo_tmux_send.py"
            tmux_send.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
message_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --message-file) message_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
prompt_file=$(sed -n 's/^Read the dispatch prompt from \\(.*\\) and follow it exactly\\.$/\\1/p' "$message_file")
cp "$prompt_file" "$OMO_CAPTURE_PROMPT"
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)
            task = root / "task.md"
            task.write_text("header\n(pending)\npayload\n> DM\n", encoding="utf-8")

            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "2",
                    "--end",
                    "4",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home), "OMO_CAPTURE_PROMPT": str(capture)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual("payload\n> DM", capture.read_text(encoding="utf-8"))

    def test_omo_dispatch_strips_repeated_edge_direct_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            capture = tmp_path / "captured-prompt.txt"
            tmux_send = bin_dir / "omo_tmux_send.py"
            tmux_send.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
message_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --message-file) message_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
prompt_file=$(sed -n 's/^Read the dispatch prompt from \\(.*\\) and follow it exactly\\.$/\\1/p' "$message_file")
cp "$prompt_file" "$OMO_CAPTURE_PROMPT"
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)
            task = root / "task.md"
            task.write_text("header\n(pending)\nDM\nDM only\npayload\nDM\nDM only\n", encoding="utf-8")

            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "2",
                    "--end",
                    "7",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home), "OMO_CAPTURE_PROMPT": str(capture)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual("payload", capture.read_text(encoding="utf-8"))

    def test_omo_dispatch_clears_only_one_matching_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            capture = tmp_path / "captured-prompt.txt"
            tmux_send = bin_dir / "omo_tmux_send.py"
            tmux_send.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
message_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --message-file) message_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
prompt_file=$(sed -n 's/^Read the dispatch prompt from \\(.*\\) and follow it exactly\\.$/\\1/p' "$message_file")
cp "$prompt_file" "$OMO_CAPTURE_PROMPT"
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)
            task = root / "task.md"
            task.write_text("header\n(pending)\nfirst\n(pending)\nsecond\n", encoding="utf-8")

            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "2",
                    "--end",
                    "5",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home), "OMO_CAPTURE_PROMPT": str(capture)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual("first\n(pending)\nsecond", capture.read_text(encoding="utf-8"))
            self.assertEqual(1, task.read_text(encoding="utf-8").count("(pending)"))

    def test_omo_dispatch_does_not_clear_pending_when_file_changes_during_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            tmux_send = bin_dir / "omo_tmux_send.py"
            task = root / "task.md"
            task.write_text("header\n(pending)\nfirst\n", encoding="utf-8")
            tmux_send.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
printf 'header\\n(pending)\\nchanged\\n' > {task}
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)

            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "2",
                    "--end",
                    "3",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("dispatch cleanup skipped: file changed during send", result.stderr)
            self.assertEqual("header\n(pending)\nchanged\n", task.read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
