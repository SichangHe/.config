#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Recover only the main Codex manager in `wl:1` after its Sol model is unavailable.

The helper deliberately has no live-picker path.  It first proves a narrowly
defined fatal state, then proves a replacement model with an isolated ephemeral
Codex request, captures the live session id, and atomically respawns that same
pane into `codex resume`.  Any failed precondition leaves the old pane alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_start import EFFORTS, MODEL_RE, SUPPORTED_CODEX_PROCESS_COMMANDS, Pane, StartError, resolve_pane, respawn_codex, wait_started
from omo_manager.omo_codex_status import CODEX_FOOTER_RE, current_block, current_input_text, is_stock_placeholder_input_text, report_from_lines, visible_error_lines
from omo_manager.omo_codex_stop import extract_new_status_session_id, input_has_status_prompt
from omo_manager.omo_manager_rotate import LaunchMetadata, read_processes, select_launch_metadata


MAIN_MANAGER_TARGET = "wl:1.0"
FAILED_MODEL = "gpt-5.6-sol"
FATAL_ERROR = f'''■ {{"detail":"The '{FAILED_MODEL}' model is not supported when using Codex with a ChatGPT account."}}'''
MAX_AUTHORITY_BYTES = 1_000_000
MAX_CAPTURE_LINES = 240
MODEL_PROBE_RESPONSE = {"available": True}
LINE_RANGE_RE = re.compile(r"^([1-9]\d*)-([1-9]\d*)$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
AUTHORITATIVE_ENVELOPE_RE = re.compile(
    r'\A<human_instruction[ \t]+authoritative="true"[ \t]+source="(?P<source>[^"\r\n]+)">\r?\n(?P<body>.*?)</human_instruction>\r?\n?\Z',
    re.DOTALL,
)
POSITIVE_RECOVERY_GRANT_RE = re.compile(
    r"main-manager-model-recovery: target=wl:1 action=resume-same-pane model=(?P<model>[A-Za-z0-9._-]+) replacement-pane=forbidden"
)


class RecoveryError(RuntimeError):
    """A main-manager model recovery safety gate failed."""


@dataclass(frozen=True)
class Args:
    root: Path
    model: str
    authority_file: Path
    authority_lines: tuple[int, int]
    authority_envelope: Path
    handoff_output: Path
    startup_timeout_s: float
    model_probe_timeout_s: float
    dry_run: bool


@dataclass(frozen=True)
class Authority:
    source_path: Path
    lines: tuple[int, int]
    source_device: int
    source_inode: int
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    envelope_path: Path
    envelope_device: int
    envelope_inode: int
    envelope_size: int
    envelope_mtime_ns: int
    envelope_sha256: str


@dataclass(frozen=True)
class ManagerEnvironment:
    work_log_root: Path
    state_dir: Path


@dataclass(frozen=True)
class Binding:
    pane: Pane
    launch: LaunchMetadata
    environment: ManagerEnvironment


@dataclass(frozen=True)
class Handoff:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class PrivateFile:
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str
    text: str


class ParsedArgs(argparse.Namespace):
    root: Path = Path()
    model: str = ""
    authority_file: Path = Path()
    authority_lines: tuple[int, int] = (1, 1)
    authority_envelope: Path = Path()
    handoff_output: Path = Path()
    startup_timeout_s: float = 45.0
    model_probe_timeout_s: float = 45.0
    dry_run: bool = False


def parse_line_range(value: str) -> tuple[int, int]:
    match = LINE_RANGE_RE.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("must be START-END with positive, inclusive line numbers")
    start, end = (int(part) for part in match.groups())
    if start > end:
        raise argparse.ArgumentTypeError("START must not exceed END")
    return start, end


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--model", required=True, help="Replacement Codex model, proven by an isolated account probe before pane replacement.")
    _ = parser.add_argument("--authority-file", type=Path, required=True, help="Authoritative human-email source under ROOT/manager_mail.")
    _ = parser.add_argument("--authority-lines", type=parse_line_range, required=True, help="Inclusive source lines that authorize the same-pane recovery.")
    _ = parser.add_argument("--authority-envelope", type=Path, required=True, help="Owner-private task envelope containing the exact authoritative human-instruction block.")
    _ = parser.add_argument("--handoff-output", type=Path, required=True, help="New owner-private handoff record; its parent must already be private.")
    _ = parser.add_argument("--startup-timeout-s", type=float, default=45.0)
    _ = parser.add_argument("--model-probe-timeout-s", type=float, default=45.0)
    _ = parser.add_argument("--dry-run", action="store_true", help="Run every non-pane-replacement gate, including the isolated model probe.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if MODEL_RE.fullmatch(parsed.model) is None:
        parser.error("--model contains unsupported characters")
    if parsed.model == FAILED_MODEL:
        parser.error(f"--model must differ from unavailable {FAILED_MODEL}")
    for name in ("startup_timeout_s", "model_probe_timeout_s"):
        value = getattr(parsed, name)
        if not math.isfinite(value) or not 0 < value <= 120:
            parser.error(f"--{name.replace('_', '-')} must be finite, positive, and at most 120 seconds")
    return Args(
        root=parsed.root.expanduser().resolve(strict=False),
        model=parsed.model,
        authority_file=parsed.authority_file.expanduser(),
        authority_lines=parsed.authority_lines,
        authority_envelope=parsed.authority_envelope.expanduser(),
        handoff_output=parsed.handoff_output.expanduser(),
        startup_timeout_s=parsed.startup_timeout_s,
        model_probe_timeout_s=parsed.model_probe_timeout_s,
        dry_run=parsed.dry_run,
    )


def identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def owner_private_directory(path: Path, label: str) -> os.stat_result:
    try:
        value = path.stat()
    except OSError as error:
        raise RecoveryError(f"{label} is unavailable: {error}") from error
    if path.is_symlink() or not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) & 0o077:
        raise RecoveryError(f"{label} must be one owner-private real directory")
    return value


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def authority_paths(args: Args) -> tuple[Path, Path, Path]:
    try:
        root = args.root.resolve(strict=True)
        mail_root = (root / "manager_mail").resolve(strict=True)
    except OSError as error:
        raise RecoveryError(f"work-log root or manager-mail directory is unavailable: {error}") from error
    _ = owner_private_directory(root, "work-log root")
    _ = owner_private_directory(mail_root, "manager-mail directory")
    candidate = args.authority_file if args.authority_file.is_absolute() else root / args.authority_file
    try:
        source = candidate.resolve(strict=True)
    except OSError as error:
        raise RecoveryError(f"authority source is unavailable: {error}") from error
    if source.parent != mail_root:
        raise RecoveryError("authority source must be one direct file under ROOT/manager_mail")
    candidate = args.authority_envelope if args.authority_envelope.is_absolute() else root / args.authority_envelope
    try:
        envelope = candidate.resolve(strict=True)
    except OSError as error:
        raise RecoveryError(f"authority envelope is unavailable: {error}") from error
    if envelope.parent != root:
        raise RecoveryError("authority envelope must be one direct owner-private file under ROOT")
    return root, source, envelope


def read_private_file(path: Path, label: str) -> PrivateFile:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise RecoveryError(f"{label} cannot be opened safely: {error}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
            raise RecoveryError(f"{label} must be one owner-private regular file")
        chunks: list[bytes] = []
        remaining = MAX_AUTHORITY_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        current = path.stat()
    except OSError as error:
        raise RecoveryError(f"{label} disappeared while it was read: {error}") from error
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise RecoveryError(f"{label} changed while it was read")
    if len(data) > MAX_AUTHORITY_BYTES:
        raise RecoveryError(f"{label} exceeds the bounded size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecoveryError(f"{label} is not UTF-8") from error
    return PrivateFile(path, before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, hashlib.sha256(data).hexdigest(), text)


def read_authority(args: Args) -> Authority:
    root, source_path, envelope_path = authority_paths(args)
    source = read_private_file(source_path, "authority source")
    envelope = read_private_file(envelope_path, "authority envelope")
    lines = source.text.splitlines(keepends=True)
    start, end = args.authority_lines
    if end > len(lines):
        raise RecoveryError("authority source line range exceeds the file")
    excerpt = "".join(lines[start - 1 : end])
    match = AUTHORITATIVE_ENVELOPE_RE.fullmatch(envelope.text)
    if match is None:
        raise RecoveryError("authority envelope must contain exactly one authoritative human-instruction block")
    expected_source = f"{source.path.relative_to(root).as_posix()}:{start}-{end}"
    if match.group("source") != expected_source or match.group("body") != excerpt:
        raise RecoveryError("authority envelope does not bind the exact selected source excerpt")
    grant = POSITIVE_RECOVERY_GRANT_RE.fullmatch(excerpt.strip())
    if grant is None or grant.group("model") != args.model:
        raise RecoveryError("authority excerpt must be one exact positive same-pane recovery grant for the requested model")
    return Authority(
        source.path,
        args.authority_lines,
        source.device,
        source.inode,
        source.size,
        source.mtime_ns,
        source.sha256,
        envelope.path,
        envelope.device,
        envelope.inode,
        envelope.size,
        envelope.mtime_ns,
        envelope.sha256,
    )


def verify_authority(args: Args, expected: Authority) -> None:
    if read_authority(args) != expected:
        raise RecoveryError("authority source changed or no longer matches before replacement")


def same_pane(expected: Pane) -> Pane:
    current = resolve_pane(MAIN_MANAGER_TARGET)
    if current != expected:
        raise RecoveryError("wl:1 pane, window, process, command, or working directory changed")
    return current


def pane_guard_condition(pane: Pane) -> str:
    """Return the tmux-server condition for one pinned pane process."""

    return "#{&&:#{==:#{pane_id},%s},#{==:#{window_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}" % (
        pane.pane_id,
        pane.window_id,
        pane.target,
        pane.pane_pid,
        pane.command,
    )


def tmux_run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded tmux command for this helper's exact-pane operations."""

    try:
        return subprocess.run(["tmux", *command], capture_output=True, text=True, timeout=5.0, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RecoveryError(f"tmux operation failed safely: {error}") from error


def capture_text_lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def guarded_capture(pane: Pane) -> str:
    """Capture only while the fixed target still resolves to the pinned process."""

    _ = same_pane(pane)
    captured = tmux_run(["capture-pane", "-p", "-t", pane.pane_id, "-S", f"-{MAX_CAPTURE_LINES}"])
    if captured.returncode != 0:
        detail = captured.stderr.strip() or "capture-pane failed"
        raise RecoveryError(f"could not capture pinned wl:1 output: {detail}")
    _ = same_pane(pane)
    return captured.stdout


def guarded_tmux_input(pane: Pane, command: str, operation: str) -> None:
    """Submit fixed input only if tmux still binds the exact pane at execution."""

    _ = same_pane(pane)
    result = tmux_run(["if-shell", "-F", "-t", pane.pane_id, pane_guard_condition(pane), command, "run-shell 'exit 1'"])
    if result.returncode != 0:
        detail = result.stderr.strip() or f"pane identity changed before {operation}"
        raise RecoveryError(f"refused {operation} in wl:1: {detail}")
    _ = same_pane(pane)


def guarded_status_session_id(pane: Pane, wait_s: float, before_input: Callable[[], None]) -> tuple[str, str]:
    """Return a newly-rendered status UUID without an unguarded pane paste or Enter."""

    before = guarded_capture(pane)
    input_text = current_input_text(capture_text_lines(before))
    if input_text.strip() and not is_stock_placeholder_input_text(input_text):
        raise RecoveryError("wl:1 has existing input that status recovery must preserve")
    buffer_name = f"omo-main-manager-status-{os.getpid()}-{time.monotonic_ns()}"
    loaded = tmux_run(["set-buffer", "-b", buffer_name, "/status"])
    if loaded.returncode != 0:
        detail = loaded.stderr.strip() or "set-buffer failed"
        raise RecoveryError(f"could not prepare guarded wl:1 status query: {detail}")
    paste_and_submit = "paste-buffer -b %s -t %s \\; send-keys -t %s Enter" % (
        shlex.quote(buffer_name),
        shlex.quote(pane.pane_id),
        shlex.quote(pane.pane_id),
    )
    try:
        before_input()
        guarded_tmux_input(pane, paste_and_submit, "status paste and Enter")
    finally:
        try:
            _ = tmux_run(["delete-buffer", "-b", buffer_name])
        except RecoveryError:
            pass
    deadline = time.monotonic() + wait_s
    after = before
    fallback_sent = False
    while time.monotonic() < deadline:
        after = guarded_capture(pane)
        session_id = extract_new_status_session_id(before, after)
        if session_id:
            return session_id, after
        if not fallback_sent and input_has_status_prompt(after):
            send_enter = "send-keys -t %s Enter" % shlex.quote(pane.pane_id)
            before_input()
            guarded_tmux_input(pane, send_enter, "status fallback Enter")
            fallback_sent = True
        time.sleep(0.25)
    return "", after


def captured_lines(pane: Pane) -> list[str]:
    lines = capture_text_lines(guarded_capture(pane))
    if not lines:
        raise RecoveryError("wl:1 produced no current Codex output")
    return lines


def require_fatal_state(pane: Pane) -> None:
    lines = captured_lines(pane)
    if report_from_lines(lines).status != "error":
        raise RecoveryError("wl:1 is not in the required Codex error state")
    errors = tuple(visible_error_lines(current_block(lines).lines))
    if errors != (FATAL_ERROR,):
        raise RecoveryError("wl:1 does not have exactly the supported gpt-5.6-sol ChatGPT-account failure")
    if not any(CODEX_FOOTER_RE.match(line) is not None for line in lines):
        raise RecoveryError("wl:1 lacks a live Codex model footer")
    input_text = current_input_text(lines)
    if input_text.strip() and not is_stock_placeholder_input_text(input_text):
        raise RecoveryError("wl:1 has existing input that recovery must preserve")


def live_launch(pane: Pane, expected_model: str = FAILED_MODEL) -> LaunchMetadata:
    try:
        launch = select_launch_metadata(read_processes(), pane.pane_pid, None, None)
    except Exception as error:
        raise RecoveryError(f"could not bind one live Codex launch under wl:1: {error}") from error
    if launch.model != expected_model or launch.reasoning_effort not in EFFORTS or launch.launch_pid is None:
        raise RecoveryError(f"wl:1 launch is not the expected {expected_model} manager process")
    return launch


def process_environment(pid: int) -> dict[str, str]:
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as error:
        raise RecoveryError(f"could not read the live manager launch environment: {error}") from error
    values: dict[str, str] = {}
    for item in data.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            name = key.decode("utf-8")
            rendered = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RecoveryError("live manager environment is not UTF-8") from error
        if name in values and values[name] != rendered:
            raise RecoveryError(f"live manager environment has conflicting {name}")
        values[name] = rendered
    return values


def manager_environment(args: Args, pane: Pane, launch: LaunchMetadata) -> ManagerEnvironment:
    if pane.target != MAIN_MANAGER_TARGET:
        raise RecoveryError("live process is not bound to the exact wl:1 main-manager pane")
    if launch.launch_pid is None:
        raise RecoveryError("live manager launch has no process id")
    values = process_environment(launch.launch_pid)
    if values.get("OMO_AGENT_TMUX_TARGET") != MAIN_MANAGER_TARGET or values.get("OMO_MANAGER_TMUX_TARGET") != MAIN_MANAGER_TARGET:
        raise RecoveryError("live process is not bound to the exact wl:1 main-manager role")
    try:
        work_log_root = Path(values["OMO_WORK_LOGS_ROOT"]).resolve(strict=True)
    except (KeyError, OSError) as error:
        raise RecoveryError("live manager has no readable OMO_WORK_LOGS_ROOT binding") from error
    if work_log_root != args.root:
        raise RecoveryError("live manager work-log root differs from --root")
    try:
        state_dir = Path(values["OMO_MANAGER_STATE_DIR"]).resolve(strict=True)
    except (KeyError, OSError) as error:
        raise RecoveryError("live manager has no readable OMO_MANAGER_STATE_DIR binding") from error
    _ = owner_private_directory(state_dir, "manager state directory")
    return ManagerEnvironment(work_log_root, state_dir)


def bind(args: Args) -> Binding:
    pane = resolve_pane(MAIN_MANAGER_TARGET)
    if pane.target != MAIN_MANAGER_TARGET or pane.command not in SUPPORTED_CODEX_PROCESS_COMMANDS:
        raise RecoveryError("wl:1 is not the exact live Codex manager pane")
    if os.environ.get("TMUX_PANE") == pane.pane_id:
        raise RecoveryError("run recovery from a different pane than wl:1")
    require_fatal_state(pane)
    launch = live_launch(pane)
    environment = manager_environment(args, pane, launch)
    _ = same_pane(pane)
    return Binding(pane, launch, environment)


def verify_binding(args: Args, expected: Binding) -> None:
    current = bind(args)
    if current != expected:
        raise RecoveryError("wl:1 main-manager binding changed during recovery preparation")


def probe_model(model: str, effort: str, timeout_s: float) -> None:
    with tempfile.TemporaryDirectory(prefix="omo-main-manager-model-probe-") as raw_directory:
        directory = Path(raw_directory)
        schema = directory / "schema.json"
        result = directory / "result.json"
        schema.write_text(json.dumps({"type": "object", "properties": {"available": {"const": True}}, "required": ["available"], "additionalProperties": False}) + "\n", encoding="utf-8")
        schema.chmod(0o600)
        command = [
            "bunx",
            "@openai/codex",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "--config",
            f'model_reasoning_effort="{effort}"',
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(result),
            "Return only the JSON object required by the output schema.",
        ]
        try:
            completed = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RecoveryError(f"replacement model did not pass the isolated availability probe: {error}") from error
        if completed.returncode != 0 or not result.is_file():
            raise RecoveryError("replacement model did not pass the isolated availability probe")
        try:
            payload: object = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecoveryError("replacement model availability probe produced invalid output") from error
        if payload != MODEL_PROBE_RESPONSE:
            raise RecoveryError("replacement model availability probe did not prove the expected result")


def resume_command(binding: Binding, model: str, session_id: str, marker: str) -> str:
    pane = binding.pane
    launch = [
        "bunx",
        "@openai/codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{binding.launch.reasoning_effort}"',
        "--config",
        "check_for_update_on_startup=false",
        "--config",
        'tui.resume_cwd="current"',
        "--cd",
        str(pane.workdir),
        "resume",
        session_id,
    ]
    exports = {
        "OMO_AGENT_TMUX_TARGET": pane.target,
        "OMO_MANAGER_TMUX_TARGET": pane.target,
        "OMO_MANAGER_STATE_DIR": str(binding.environment.state_dir),
        "OMO_WORK_LOGS_ROOT": str(binding.environment.work_log_root),
    }
    export_text = " ".join(f"{key}={shlex.quote(value)}" for key, value in exports.items())
    return f"export {export_text}; cd {shlex.quote(str(pane.workdir))} && printf '%s\\n' {shlex.quote(marker)} && exec {shlex.join(launch)}"


def handoff_text(binding: Binding, args: Args, authority: Authority, session_id: str) -> str:
    return "\n".join(
        (
            "version: omo-main-manager-model-recovery-v1",
            "phase: prepared",
            f"target: {binding.pane.target}",
            f"pane-id: {binding.pane.pane_id}",
            f"window-id: {binding.pane.window_id}",
            f"old-pane-pid: {binding.pane.pane_pid}",
            f"old-command: {binding.pane.command}",
            f"old-model: {binding.launch.model}",
            f"new-model: {args.model}",
            f"reasoning-effort: {binding.launch.reasoning_effort}",
            f"session-id: {session_id}",
            f"workdir: {binding.pane.workdir}",
            f"manager-state-dir: {binding.environment.state_dir}",
            f"authority-source: {authority.source_path}",
            f"authority-lines: {authority.lines[0]}-{authority.lines[1]}",
            f"authority-sha256: {authority.source_sha256}",
            f"authority-envelope: {authority.envelope_path}",
            f"authority-envelope-sha256: {authority.envelope_sha256}",
            f"fatal-error-sha256: {hashlib.sha256(FATAL_ERROR.encode()).hexdigest()}",
            f"prepared-at-ns: {time.time_ns()}",
            "",
        )
    )


def reserve_handoff(path: Path, text: str) -> Handoff:
    destination = path.resolve(strict=False)
    _ = owner_private_directory(destination.parent, "handoff parent directory")
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise RecoveryError(f"could not reserve a new handoff record: {error}") from error
    try:
        os.fchmod(fd, 0o600)
        write_all(fd, text.encode("utf-8"))
        os.fsync(fd)
        value = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) & 0o077:
        raise RecoveryError("reserved handoff record is not owner-private")
    return Handoff(destination, value.st_dev, value.st_ino)


def finish_handoff(handoff: Handoff, outcome: str, detail: str) -> None:
    try:
        fd = os.open(handoff.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    except OSError as error:
        raise RecoveryError(f"could not finalize the handoff record: {error}") from error
    try:
        value = os.fstat(fd)
        if (value.st_dev, value.st_ino) != (handoff.device, handoff.inode):
            raise RecoveryError("handoff record identity changed before finalization")
        write_all(fd, f"final-outcome: {outcome}\nfinal-detail: {detail}\n".encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def capture_session(args: Args, binding: Binding, authority: Authority) -> str:
    verify_binding(args, binding)
    session_id, _output = guarded_status_session_id(
        binding.pane,
        min(10.0, args.startup_timeout_s),
        lambda: verify_authority(args, authority),
    )
    if UUID_RE.fullmatch(session_id) is None:
        raise RecoveryError("could not capture one valid current Codex session id; wl:1 was not replaced")
    verify_binding(args, binding)
    return session_id


def verify_continuity(args: Args, expected: Binding, authority: Authority, session_id: str) -> None:
    current = resolve_pane(MAIN_MANAGER_TARGET)
    if (
        current.target != expected.pane.target
        or current.pane_id != expected.pane.pane_id
        or current.window_id != expected.pane.window_id
        or current.pane_pid == expected.pane.pane_pid
        or current.workdir != expected.pane.workdir
    ):
        raise RecoveryError("same-pane recovery did not produce one new process in wl:1")
    if current.command not in SUPPORTED_CODEX_PROCESS_COMMANDS:
        raise RecoveryError("same-pane recovery did not leave a live Codex process")
    launch = live_launch(current, args.model)
    if launch.model != args.model or launch.reasoning_effort != expected.launch.reasoning_effort:
        raise RecoveryError("resumed manager did not retain the requested model and prior reasoning effort")
    if manager_environment(args, current, launch) != expected.environment:
        raise RecoveryError("resumed manager did not retain the original manager environment")
    resumed_id, _output = guarded_status_session_id(
        current,
        min(10.0, args.startup_timeout_s),
        lambda: verify_authority(args, authority),
    )
    if resumed_id != session_id:
        raise RecoveryError("resumed manager did not prove continuity with the captured session id")


def recover(args: Args) -> str:
    authority = read_authority(args)
    binding = bind(args)
    probe_model(args.model, binding.launch.reasoning_effort, args.model_probe_timeout_s)
    verify_binding(args, binding)
    if args.dry_run:
        verify_authority(args, authority)
        verify_binding(args, binding)
        return "dry-run"
    session_id = capture_session(args, binding, authority)
    verify_authority(args, authority)
    verify_binding(args, binding)
    handoff = reserve_handoff(args.handoff_output, handoff_text(binding, args, authority, session_id))
    replacement_attempted = False
    try:
        verify_authority(args, authority)
        verify_binding(args, binding)
        marker = f"[omo-main-manager-model-recovery:{os.getpid()}:{time.time_ns()}]"
        replacement_attempted = True
        respawn_codex(binding.pane, resume_command(binding, args.model, session_id, marker))
        result = wait_started(binding.pane, marker, args.startup_timeout_s)
        verify_continuity(args, binding, authority, session_id)
    except Exception as error:
        outcome = "completion-unknown" if replacement_attempted else "not-replaced"
        try:
            finish_handoff(handoff, outcome, type(error).__name__)
        except RecoveryError as handoff_error:
            raise RecoveryError(f"{error}; {handoff_error}") from error
        raise
    finish_handoff(handoff, "succeeded", result)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        result = recover(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, RecoveryError, StartError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"omo_main_manager_model_recovery: {error}", file=sys.stderr)
        return 1
    print(f"omo_main_manager_model_recovery: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
