#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Replace the Codex manager in one exact tmux pane with a fresh session."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

HELPER_DIR = Path(__file__).resolve().parent
WORKER_DEFAULTS = HELPER_DIR / "WORKER_DEFAULTS.md"
STATUS_HELPER = HELPER_DIR / "omo_codex_status.py"
WATCHER_HELPER = HELPER_DIR / "omo_manager_setup_watchers.sh"
TARGET_RE = re.compile(r"^(?P<session>[A-Za-z][A-Za-z0-9_-]*):(?P<window>0|[1-9][0-9]*)(?:\.(?P<pane>0|[1-9][0-9]*))?$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")
EFFORT_RE = re.compile(r"^model_reasoning_effort\s*=\s*(['\"]?)([A-Za-z0-9_-]+)\1$")
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
SUCCESS_STATUSES = {"ready", "running"}
TERMINAL_FAILURE_STATUSES = {"error"}
HANDOFF_TIMEOUT_S = 10.0
HANDOFF_LOCK_TIMEOUT_S = 10.0
RESERVATION_NAME = "manager-rotation.handoff.json"
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
CODEX_PACKAGE = "@openai/codex@latest"
SUPPORTED_CODEX_PACKAGES = {"@openai/codex", CODEX_PACKAGE}


class RotationError(RuntimeError):
    """A safety check or rotation operation failed."""


@dataclass(frozen=True)
class Args:
    target: str
    root: Path
    state_dir: Path
    model: str | None
    reasoning_effort: str | None
    startup_timeout_s: float
    poll_interval_s: float
    coordinator_token: str | None = None


@dataclass(frozen=True)
class PaneIdentity:
    canonical_target: str
    pane_id: str
    window_id: str
    pane_pid: int
    working_directory: Path


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    state: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class LaunchMetadata:
    model: str
    reasoning_effort: str
    source: str
    launch_pid: int | None
    launch_argv: tuple[str, ...]


@dataclass(frozen=True)
class Preflight:
    args: Args
    pane: PaneIdentity
    metadata: LaunchMetadata
    pane_output: str
    prompt: str
    invoked_from_target: bool = False


@dataclass(frozen=True)
class Coordinator:
    pane_id: str
    token: str
    log_path: Path


@dataclass(frozen=True)
class RotationResult:
    path: Path
    coordinated: bool


@dataclass(frozen=True)
class HandoffReservation:
    token: str
    pane_id: str
    phase: str


class ParsedArgs(argparse.Namespace):
    target: str | None = None
    root: Path | None = None
    state_dir: Path | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    startup_timeout_s: float = 45.0
    poll_interval_s: float = 0.5
    coordinator_token: str | None = None


def default_state_dir() -> Path:
    return Path(os.environ.get("OMO_MANAGER_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--target", default=os.environ.get("OMO_MANAGER_TMUX_TARGET"), help="Exact SESSION:WINDOW[.PANE] target (default: OMO_MANAGER_TMUX_TARGET).")
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--state-dir", type=Path, default=default_state_dir(), help="Private audit state (default: OMO_MANAGER_STATE_DIR or XDG state).")
    _ = parser.add_argument("--model", help="Required with --reasoning-effort only when live metadata is unavailable.")
    _ = parser.add_argument("--reasoning-effort", choices=sorted(EFFORTS), help="Required with --model only when live metadata is unavailable.")
    _ = parser.add_argument("--startup-timeout-s", type=float, default=45.0)
    _ = parser.add_argument("--poll-interval-s", type=float, default=0.5)
    _ = parser.add_argument("--_coordinator-token", dest="coordinator_token", help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.target:
        parser.error("--target is required when OMO_MANAGER_TMUX_TARGET is unset.")
    if (parsed.model is None) != (parsed.reasoning_effort is None):
        parser.error("--model and --reasoning-effort must be supplied together.")
    if parsed.model is not None and MODEL_RE.fullmatch(parsed.model) is None:
        parser.error("--model contains unsupported characters.")
    if parsed.startup_timeout_s <= 0:
        parser.error("--startup-timeout-s must be positive.")
    if parsed.poll_interval_s <= 0:
        parser.error("--poll-interval-s must be positive.")
    if parsed.coordinator_token is not None and TOKEN_RE.fullmatch(parsed.coordinator_token) is None:
        parser.error("internal coordinator token is invalid.")
    if parsed.coordinator_token is not None and (parsed.model is None or parsed.reasoning_effort is None):
        parser.error("internal coordinator requires explicit --model and --reasoning-effort.")
    assert parsed.root is not None and parsed.state_dir is not None
    return Args(
        parsed.target,
        parsed.root.expanduser().resolve(),
        parsed.state_dir.expanduser().resolve(),
        parsed.model,
        parsed.reasoning_effort,
        parsed.startup_timeout_s,
        parsed.poll_interval_s,
        parsed.coordinator_token,
    )


def run(command: list[str], *, timeout: float = 10, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=env)


def resolve_exact_pane(target: str) -> PaneIdentity:
    match = TARGET_RE.fullmatch(target)
    if match is None:
        raise RotationError("target must be a numeric tmux target: SESSION:WINDOW[.PANE]")
    result = run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            target,
            "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{window_id}\t#{window_panes}\t#{pane_pid}\t#{pane_current_path}",
        ],
        timeout=5,
    )
    if result.returncode != 0:
        raise RotationError(f"tmux target does not resolve: {target}: {result.stderr.strip()}")
    fields = result.stdout.rstrip("\n").split("\t", 7)
    if len(fields) != 8:
        raise RotationError(f"tmux returned malformed pane metadata for {target}")
    session, window, pane, pane_id, window_id, raw_window_panes, raw_pid, raw_cwd = fields
    canonical = f"{session}:{window}.{pane}"
    requested_window = f"{match.group('session')}:{match.group('window')}"
    resolved_window = f"{session}:{window}"
    shorthand_is_exact = match.group("pane") is None and resolved_window == requested_window and pane == "0" and raw_window_panes == "1"
    full_target_is_exact = match.group("pane") is not None and canonical == target
    if not (shorthand_is_exact or full_target_is_exact) or not pane_id.startswith("%") or not window_id.startswith("@"):
        raise RotationError(f"tmux resolved {target!r} as {canonical!r}; refusing an ambiguous or non-exact target")
    try:
        pane_pid = int(raw_pid)
    except ValueError as exc:
        raise RotationError(f"tmux returned an invalid pane PID for {target}") from exc
    working_directory = Path(raw_cwd)
    if not working_directory.is_dir():
        raise RotationError(f"target pane working directory is unavailable: {working_directory}")
    return PaneIdentity(canonical, pane_id, window_id, pane_pid, working_directory)


def read_processes(proc_root: Path = Path("/proc")) -> dict[int, ProcessInfo]:
    processes: dict[int, ProcessInfo] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise RotationError(f"cannot inspect {proc_root}: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            rest = stat.rsplit(")", 1)[1].split()
            state, ppid = rest[0], int(rest[1])
            raw_argv = (entry / "cmdline").read_bytes()
        except (IndexError, OSError, ValueError):
            continue
        argv = tuple(part.decode(errors="replace") for part in raw_argv.split(b"\0") if part)
        processes[int(entry.name)] = ProcessInfo(int(entry.name), ppid, state, argv)
    return processes


def process_is_under(pid: int, ancestor_pid: int, processes: dict[int, ProcessInfo]) -> bool:
    current = pid
    seen: set[int] = set()
    while current > 1 and current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        process = processes.get(current)
        if process is None:
            return False
        current = process.ppid
    return False


def is_codex_launch_argv(argv: tuple[str, ...]) -> bool:
    if len(argv) >= 2 and Path(argv[0]).name == "bunx" and argv[1] in SUPPORTED_CODEX_PACKAGES:
        return True
    return len(argv) >= 3 and Path(argv[0]).name == "bun" and argv[1] in {"x", "bunx"} and argv[2] in SUPPORTED_CODEX_PACKAGES


def option_values(argv: tuple[str, ...]) -> tuple[list[str], list[str]]:
    package_index = next((index for index, arg in enumerate(argv) if arg in SUPPORTED_CODEX_PACKAGES), None)
    if package_index is None:
        raise RotationError("Codex launch argv is missing a supported @openai/codex package")
    models: list[str] = []
    efforts: list[str] = []
    index = package_index + 1
    while index < len(argv):
        token = argv[index]
        value: str | None = None
        if token in {"--model", "-m"}:
            index += 1
            if index >= len(argv):
                raise RotationError(f"Codex launch argv has {token} without a value")
            models.append(argv[index])
        elif token.startswith("--model="):
            models.append(token.partition("=")[2])
        elif token.startswith("-m") and token != "-m":
            models.append(token[2:])
        elif token in {"--config", "-c"}:
            index += 1
            if index >= len(argv):
                raise RotationError(f"Codex launch argv has {token} without a value")
            value = argv[index]
        elif token.startswith("--config="):
            value = token.removeprefix("--config=")
        elif token.startswith("-c") and token != "-c":
            value = token[2:]
        elif not token.startswith("-"):
            break
        if value is not None and (effort_match := EFFORT_RE.fullmatch(value)) is not None:
            efforts.append(effort_match.group(2))
        index += 1
    return models, efforts


def unique_metadata_value(values: list[str], label: str) -> str | None:
    if not values:
        return None
    if len(values) > 1:
        kind = "conflicting" if len(set(values)) > 1 else "ambiguous duplicate"
        raise RotationError(f"Codex launch argv has {kind} {label} metadata: {values}")
    value = values[0]
    if not value:
        raise RotationError(f"Codex launch argv has empty {label} metadata")
    return value


def select_launch_metadata(
    processes: dict[int, ProcessInfo],
    pane_pid: int,
    model_override: str | None,
    effort_override: str | None,
    *,
    validated_override: bool = False,
) -> LaunchMetadata:
    launches = [process for process in processes.values() if process.state != "Z" and process_is_under(process.pid, pane_pid, processes) and is_codex_launch_argv(process.argv)]
    if len(launches) > 1:
        raise RotationError(f"found multiple live Codex launch argv descendants under pane PID {pane_pid}: {[process.pid for process in launches]}")

    inferred_model: str | None = None
    inferred_effort: str | None = None
    launch = launches[0] if launches else None
    if launch is not None:
        models, efforts = option_values(launch.argv)
        inferred_model = unique_metadata_value(models, "model")
        inferred_effort = unique_metadata_value(efforts, "reasoning-effort")
        if inferred_model is not None and MODEL_RE.fullmatch(inferred_model) is None:
            raise RotationError(f"inferred model contains unsupported characters: {inferred_model!r}")
        if inferred_effort is not None and inferred_effort not in EFFORTS:
            raise RotationError(f"inferred unsupported reasoning effort: {inferred_effort!r}")

    inference_complete = inferred_model is not None and inferred_effort is not None
    overrides_supplied = model_override is not None and effort_override is not None
    if validated_override:
        if not overrides_supplied:
            raise RotationError("internal coordinator requires explicit validated model and reasoning effort")
        assert model_override is not None and effort_override is not None
        if inferred_model is not None and inferred_model != model_override:
            raise RotationError(f"coordinator model {model_override!r} conflicts with inferred model {inferred_model!r}")
        if inferred_effort is not None and inferred_effort != effort_override:
            raise RotationError(f"coordinator reasoning effort {effort_override!r} conflicts with inferred effort {inferred_effort!r}")
        return LaunchMetadata(model_override, effort_override, "coordinator", launch.pid if launch else None, launch.argv if launch else ())
    if inference_complete and overrides_supplied:
        raise RotationError("--model and --reasoning-effort are allowed only when live launch metadata cannot be inferred")
    if inference_complete:
        assert inferred_model is not None and inferred_effort is not None and launch is not None
        return LaunchMetadata(inferred_model, inferred_effort, "inferred", launch.pid, launch.argv)
    if not overrides_supplied:
        raise RotationError("live model/reasoning metadata is unavailable; both --model and --reasoning-effort are required")
    assert model_override is not None and effort_override is not None
    if inferred_model is not None and inferred_model != model_override:
        raise RotationError(f"--model {model_override!r} conflicts with inferred model {inferred_model!r}")
    if inferred_effort is not None and inferred_effort != effort_override:
        raise RotationError(f"--reasoning-effort {effort_override!r} conflicts with inferred effort {inferred_effort!r}")
    return LaunchMetadata(model_override, effort_override, "override", launch.pid if launch else None, launch.argv if launch else ())


def readable_text(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RotationError(f"cannot read {label} {path}: {exc}") from exc
    if not text.strip():
        raise RotationError(f"{label} is empty: {path}")
    return text


def capture_pane(pane_id: str) -> str:
    result = run(["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-"], timeout=10)
    if result.returncode != 0:
        raise RotationError(f"failed to capture prior pane output: {result.stderr.strip()}")
    return result.stdout


def invocation_is_target(pane: PaneIdentity, processes: dict[int, ProcessInfo]) -> bool:
    return os.environ.get("TMUX_PANE", "") == pane.pane_id or process_is_under(os.getpid(), pane.pane_pid, processes)


def preflight(args: Args) -> Preflight:
    pane = resolve_exact_pane(args.target)
    if pane.canonical_target.partition(":")[0].startswith("h"):
        raise RotationError("manager rotation cannot modify a human-owned `h*` tmux session.")
    processes = read_processes()
    invoked_from_target = invocation_is_target(pane, processes)
    if args.coordinator_token is not None and invoked_from_target:
        raise RotationError(f"internal coordinator pane must differ from target pane {pane.canonical_target}")
    metadata = select_launch_metadata(
        processes,
        pane.pane_pid,
        args.model,
        args.reasoning_effort,
        validated_override=args.coordinator_token is not None,
    )
    if shutil.which("bunx") is None:
        raise RotationError("bunx is not available on PATH")
    if not STATUS_HELPER.is_file() or not WATCHER_HELPER.is_file():
        raise RotationError("required status or watcher helper is missing")
    worker_defaults = readable_text(WORKER_DEFAULTS, "worker defaults")
    manager_instructions = readable_text(args.root / "MANAGER.md", "manager instructions")
    prompt = f"{worker_defaults.rstrip()}\n\n{manager_instructions.rstrip()}\n"
    pane_output = capture_pane(pane.pane_id)
    if resolve_exact_pane(args.target) != pane:
        raise RotationError("target pane identity or launch context changed during preflight")
    return Preflight(args, pane, metadata, pane_output, prompt, invoked_from_target)


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RotationError(f"private state path is not a real directory: {path}")
    path.chmod(0o700)


def reservation_path(state_dir: Path) -> Path:
    return state_dir / RESERVATION_NAME


def read_reservation(state_dir: Path) -> HandoffReservation | None:
    path = reservation_path(state_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RotationError(f"cannot read handoff reservation {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"token", "pane_id", "phase"}:
        raise RotationError(f"invalid handoff reservation: {path}")
    token, pane_id, phase = payload["token"], payload["pane_id"], payload["phase"]
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None or not isinstance(pane_id, str) or not isinstance(phase, str):
        raise RotationError(f"invalid handoff reservation: {path}")
    return HandoffReservation(token, pane_id, phase)


def pane_exists(pane_id: str) -> bool:
    if not pane_id.startswith("%"):
        return False
    result = run(["tmux", "display-message", "-p", "-t", pane_id, "#{pane_id}"], timeout=5)
    return result.returncode == 0 and result.stdout.strip() == pane_id


def rotation_lock_is_free(state_dir: Path) -> bool:
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(state_dir / "manager-rotation.lock", flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def reject_or_clear_stale_reservation(state_dir: Path, matching_token: str | None = None) -> None:
    reservation = read_reservation(state_dir)
    if reservation is None:
        if matching_token is not None:
            raise RotationError("matching manager rotation handoff reservation is missing")
        return
    if reservation.token == matching_token:
        return
    if not reservation.pane_id and rotation_lock_is_free(state_dir):
        reservation_path(state_dir).unlink(missing_ok=True)
        return
    if reservation.pane_id and not pane_exists(reservation.pane_id):
        try:
            reservation_path(state_dir).unlink()
        except FileNotFoundError:
            pass
        return
    raise RotationError(f"active manager rotation handoff is reserved for coordinator pane {reservation.pane_id or '<starting>'}")


def acquire_lock(state_dir: Path, *, matching_token: str | None = None, timeout_s: float = 0.0) -> int:
    ensure_private_directory(state_dir)
    reject_or_clear_stale_reservation(state_dir, matching_token)
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(state_dir / "manager-rotation.lock", flags, 0o600)
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.05, max(0.01, deadline - time.monotonic())))
    except (BlockingIOError, OSError) as exc:
        try:
            os.close(fd)
        except UnboundLocalError:
            pass
        raise RotationError(f"another manager rotation holds {state_dir / 'manager-rotation.lock'}") from exc
    return fd


def write_private(path: Path, content: str, *, replace: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL) | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            _ = handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def reservation_content(reservation: HandoffReservation) -> str:
    return json.dumps(asdict(reservation), ensure_ascii=True, sort_keys=True) + "\n"


def create_reservation(state_dir: Path, token: str) -> None:
    ensure_private_directory(state_dir)
    write_private(reservation_path(state_dir), reservation_content(HandoffReservation(token, "", "reserved")))


def update_reservation(state_dir: Path, token: str, pane_id: str, phase: str) -> None:
    current = read_reservation(state_dir)
    if current is None or current.token != token:
        raise RotationError("manager rotation handoff reservation changed unexpectedly")
    temporary = state_dir / f".{RESERVATION_NAME}.{token}.tmp"
    try:
        write_private(temporary, reservation_content(HandoffReservation(token, pane_id, phase)))
        os.replace(temporary, reservation_path(state_dir))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def clear_reservation(state_dir: Path, token: str) -> None:
    current = read_reservation(state_dir)
    if current is None:
        return
    if current.token != token:
        raise RotationError("refusing to clear a different manager rotation handoff reservation")
    reservation_path(state_dir).unlink()


def fresh_command(metadata: LaunchMetadata, prompt_path: Path, target: str, root: Path, state_dir: Path) -> str:
    command = [
        "bunx",
        CODEX_PACKAGE,
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        metadata.model,
        "--config",
        f'model_reasoning_effort="{metadata.reasoning_effort}"',
    ]
    exports = {
        "OMO_AGENT_TMUX_TARGET": target,
        "OMO_MANAGER_TMUX_TARGET": target,
        "OMO_MANAGER_STATE_DIR": str(state_dir),
        "OMO_WORK_LOGS_ROOT": str(root),
    }
    export_command = " ".join(f"{name}={shlex.quote(value)}" for name, value in exports.items())
    rendered = f'export {export_command} && exec {shlex.join(command)} "$(cat -- {shlex.quote(str(prompt_path))})"'
    if "resume" in rendered.casefold() or UUID_RE.search(rendered) is not None:
        raise RotationError("fresh launch command unexpectedly contains resume or a session UUID")
    return rendered


def status_classification(target: str) -> str:
    result = run([sys.executable, str(STATUS_HELPER), target], timeout=10)
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("status: "):
            return line.removeprefix("status: ").strip()
    return ""


def verify_same_pane(expected: PaneIdentity, target: str) -> None:
    current = resolve_exact_pane(target)
    if current.pane_id != expected.pane_id or current.window_id != expected.window_id or current.canonical_target != expected.canonical_target:
        raise RotationError(f"tmux pane/window identity changed after respawn: expected {expected.pane_id}/{expected.window_id}, got {current.pane_id}/{current.window_id}")


def wait_for_startup(prepared: Preflight) -> str:
    deadline = time.monotonic() + prepared.args.startup_timeout_s
    time.sleep(min(prepared.args.poll_interval_s, prepared.args.startup_timeout_s))
    while time.monotonic() < deadline:
        verify_same_pane(prepared.pane, prepared.args.target)
        classification = status_classification(prepared.pane.canonical_target)
        if classification in SUCCESS_STATUSES:
            return classification
        if classification in TERMINAL_FAILURE_STATUSES:
            raise RotationError(f"fresh Codex startup classified as {classification}")
        time.sleep(min(prepared.args.poll_interval_s, max(0.01, deadline - time.monotonic())))
    raise RotationError("timed out waiting for omo_codex_status to classify fresh Codex as running or ready")


def audit_payload(prepared: Preflight, prompt_path: Path, command: str, outcome: str, status: str = "", error: str = "") -> dict[str, object]:
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "status": status,
        "error": error,
        "target": prepared.pane.canonical_target,
        "pane": asdict(prepared.pane) | {"working_directory": str(prepared.pane.working_directory)},
        "root": str(prepared.args.root),
        "prompt_path": str(prompt_path),
        "launch": asdict(prepared.metadata) | {"launch_argv": list(prepared.metadata.launch_argv)},
        "fresh_command": command,
        "prior_pane_output": prepared.pane_output,
    }


def write_audit(path: Path, payload: dict[str, object], *, replace: bool = False) -> None:
    write_private(path, json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", replace=replace)


def handoff_channel(token: str, phase: str) -> str:
    return f"omo-manager-rotate-{token}-{phase}"


def coordinator_command(prepared: Preflight, token: str, log_path: Path) -> str:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--target",
        prepared.pane.canonical_target,
        "--root",
        str(prepared.args.root),
        "--state-dir",
        str(prepared.args.state_dir),
        "--model",
        prepared.metadata.model,
        "--reasoning-effort",
        prepared.metadata.reasoning_effort,
        "--startup-timeout-s",
        str(prepared.args.startup_timeout_s),
        "--poll-interval-s",
        str(prepared.args.poll_interval_s),
        "--_coordinator-token",
        token,
    ]
    helper_command = shlex.join(command)
    return f'{{ tmux set-option -p -t "$TMUX_PANE" remain-on-exit off && {helper_command}; }} >> {shlex.quote(str(log_path))} 2>&1'


def spawn_coordinator(prepared: Preflight, token: str) -> Coordinator:
    coordinators_dir = prepared.args.state_dir / "coordinators"
    ensure_private_directory(coordinators_dir)
    record_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.{time.time_ns()}-{os.getpid()}"
    log_path = coordinators_dir / f"manager-rotation-coordinator-{record_id}.log"
    write_private(log_path, "")
    session = prepared.pane.canonical_target.partition(":")[0]
    result = run(
        [
            "tmux",
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"{session}:",
            "-n",
            "manager-rotate-coordinator",
            coordinator_command(prepared, token, log_path),
        ],
        timeout=10,
    )
    if result.returncode != 0:
        raise RotationError(f"failed to start detached rotation coordinator: {result.stderr.strip()}; log: {log_path}")
    coordinator_pane = result.stdout.strip()
    if not coordinator_pane.startswith("%") or coordinator_pane == prepared.pane.pane_id:
        if coordinator_pane.startswith("%"):
            _ = run(["tmux", "kill-pane", "-t", coordinator_pane], timeout=5)
        raise RotationError(f"tmux returned invalid coordinator pane {coordinator_pane!r}; target pane is {prepared.pane.pane_id}")
    return Coordinator(coordinator_pane, token, log_path)


def tmux_handoff(command: list[str], *, timeout: float = HANDOFF_TIMEOUT_S) -> None:
    try:
        result = run(command, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RotationError(f"timed out waiting for coordinator handoff: {shlex.join(command)}") from exc
    if result.returncode != 0:
        raise RotationError(f"tmux coordinator handoff failed: {result.stderr.strip()}")


def cleanup_pre_go_handoff(state_dir: Path, coordinator: Coordinator | None, token: str) -> None:
    try:
        if coordinator is not None:
            try:
                _ = run(["tmux", "kill-pane", "-t", coordinator.pane_id], timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        clear_reservation(state_dir, token)
        for phase in ("ready", "go"):
            try:
                _ = run(["tmux", "wait-for", "-U", handoff_channel(token, phase)], timeout=5)
            except subprocess.TimeoutExpired:
                pass


def begin_coordinator_handoff(prepared: Preflight) -> Coordinator:
    token = secrets.token_hex(16)
    coordinator: Coordinator | None = None
    create_reservation(prepared.args.state_dir, token)
    try:
        for phase in ("ready", "go"):
            tmux_handoff(["tmux", "wait-for", "-L", handoff_channel(token, phase)])
        coordinator = spawn_coordinator(prepared, token)
        update_reservation(prepared.args.state_dir, token, coordinator.pane_id, "started")
        tmux_handoff(["tmux", "wait-for", "-L", handoff_channel(token, "ready")])
        reservation = read_reservation(prepared.args.state_dir)
        if reservation != HandoffReservation(token, coordinator.pane_id, "ready"):
            raise RotationError("coordinator READY acknowledgement did not match its reservation")
        tmux_handoff(["tmux", "wait-for", "-U", handoff_channel(token, "ready")])
        tmux_handoff(["tmux", "wait-for", "-U", handoff_channel(token, "go")])
    except Exception:
        cleanup_pre_go_handoff(prepared.args.state_dir, coordinator, token)
        raise
    return coordinator


def coordinator_rotation(args: Args) -> Path:
    token = args.coordinator_token
    assert token is not None
    pane = resolve_exact_pane(args.target)
    coordinator_pane = os.environ.get("TMUX_PANE", "")
    reservation = read_reservation(args.state_dir)
    if coordinator_pane == pane.pane_id or reservation != HandoffReservation(token, coordinator_pane, "started"):
        raise RotationError("coordinator pane/token does not match the active handoff reservation")
    update_reservation(args.state_dir, token, coordinator_pane, "ready")
    tmux_handoff(["tmux", "wait-for", "-U", handoff_channel(token, "ready")])
    tmux_handoff(["tmux", "wait-for", "-L", handoff_channel(token, "go")])
    tmux_handoff(["tmux", "wait-for", "-U", handoff_channel(token, "go")])
    lock_fd = acquire_lock(args.state_dir, matching_token=token, timeout_s=HANDOFF_LOCK_TIMEOUT_S)
    try:
        clear_reservation(args.state_dir, token)
        return execute_rotation(preflight(args))
    finally:
        os.close(lock_fd)


def execute_rotation(prepared: Preflight) -> Path:
    rotations_dir = prepared.args.state_dir / "rotations"
    ensure_private_directory(rotations_dir)
    record_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.{time.time_ns()}-{os.getpid()}"
    prompt_path = rotations_dir / f"manager-prompt-{record_id}.txt"
    audit_path = rotations_dir / f"manager-rotation-{record_id}.json"
    write_private(prompt_path, prepared.prompt)
    command = fresh_command(prepared.metadata, prompt_path, prepared.pane.canonical_target, prepared.args.root, prepared.args.state_dir)
    write_audit(audit_path, audit_payload(prepared, prompt_path, command, "preflight-complete"))

    try:
        result = run(
            ["tmux", "respawn-pane", "-k", "-t", prepared.pane.pane_id, "-c", str(prepared.pane.working_directory), command],
            timeout=10,
        )
        if result.returncode != 0:
            raise RotationError(f"tmux respawn-pane failed: {result.stderr.strip()}")
        verify_same_pane(prepared.pane, prepared.args.target)
        classification = wait_for_startup(prepared)
        verify_same_pane(prepared.pane, prepared.args.target)
        watcher_env = os.environ.copy()
        watcher_env["OMO_WORK_LOGS_ROOT"] = str(prepared.args.root)
        watcher_env["OMO_MANAGER_TMUX_TARGET"] = prepared.pane.canonical_target
        watcher_env["OMO_MANAGER_STATE_DIR"] = str(prepared.args.state_dir)
        watcher = run([str(WATCHER_HELPER)], timeout=60, env=watcher_env)
        if watcher.returncode != 0:
            raise RotationError(f"watcher setup failed after fresh Codex startup: {watcher.stderr.strip()}")
    except Exception as exc:
        write_audit(audit_path, audit_payload(prepared, prompt_path, command, "failed", error=str(exc)), replace=True)
        raise
    write_audit(audit_path, audit_payload(prepared, prompt_path, command, "succeeded", status=classification), replace=True)
    return audit_path


def rotate(args: Args) -> RotationResult:
    lock_fd = acquire_lock(args.state_dir)
    try:
        prepared = preflight(args)
        if prepared.invoked_from_target:
            coordinator = begin_coordinator_handoff(prepared)
            result = RotationResult(coordinator.log_path, coordinated=True)
        else:
            result = RotationResult(execute_rotation(prepared), coordinated=False)
    finally:
        os.close(lock_fd)
    return result


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.coordinator_token is not None:
            result = RotationResult(coordinator_rotation(args), coordinated=False)
        else:
            result = rotate(args)
    except RotationError as exc:
        print(f"omo_manager_rotate: {exc}", file=sys.stderr)
        return 1
    label = "coordinator_log" if result.coordinated else "audit_record"
    print(f"{label}: {result.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
