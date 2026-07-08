"""Safe OpenCode quota observer and human-gated profile-switch initiator.

This helper detects low-quota/exhaustion and hang/stall signals from
non-secret, non-OpenAI-required sources and prepares a redacted, human-gated
switch plan. It never mutates live credentials,
runs real credential smoke tests, or prints credential contents.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AUTH_DIR = Path.home() / ".local" / "share" / "opencode"
DEFAULT_ROTATION_HELPER = Path.home() / ".config" / "omo_manager" / "opencode_auth_rotation_dryrun.py"
MAX_SNIPPET_CHARS = 800
LOW_PERCENT_DEFAULT = 10.0
SECRET_FIELD = r"authorization|cookie|credential|refresh(?:[_-]?token)?|access(?:[_-]?token)?|api[_-]?key|secret|session|token|key"
SECRET_VALUE_RE = re.compile(rf"(?i)([\"']?(?:{SECRET_FIELD})[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}}]+([\"']?)")
AUTH_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+")
BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
JWTISH_RE = re.compile(r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?\b")
LONG_OPAQUE_RE = re.compile(r"\b(?=[A-Za-z0-9_./+=-]*[A-Za-z])(?=[A-Za-z0-9_./+=-]*[0-9])[A-Za-z0-9_./+=-]{32,}\b")
EXHAUSTION_RE = re.compile(
    r"(?i)\b(?:quota\s*(?:exceeded|exhausted)|insufficient[_ -]?quota|usage\s*limit|rate\s*limit|billing\s*quota|out\s+of\s+credits|credit\s+balance\s+too\s+low)\b"
)
LOW_REMAINING_RE = re.compile(
    r"(?i)\b(?:remaining|left|available|quota)\b[^\n%]{0,80}?([0-9]+(?:\.[0-9]+)?)\s*%"
)
STALL_RE = re.compile(
    r"(?i)\b(?:maybe-stuck|maybe-complete-silent|manager watchdog recovery needed|unhealthy:|timeout|timed out|no progress|stalled|hung|hang|history-unavailable|session-history-not-configured|latest-turn-needs-manager)\b"
)


@dataclass(frozen=True)
class Args:
    auth_dir: Path
    current_name: str
    candidate_name: str
    all_candidates: bool
    propose_candidate: bool
    candidate_observation_file: tuple[Path, ...]
    min_high_percent: float
    check_file: tuple[Path, ...]
    quota_command: str
    stall_command: str
    health_command: str
    low_percent: float
    report_file: Path | None
    watch: bool
    interval_s: int
    max_iterations: int
    run_rotation_plan: bool
    rotation_helper: Path


class ParsedArgs(argparse.Namespace):
    auth_dir: Path = DEFAULT_AUTH_DIR
    current_name: str = "midas-team"
    candidate_name: str = ""
    all_candidates: bool = False
    propose_candidate: bool = False
    candidate_observation_file: list[Path] = []
    min_high_percent: float = 50.0
    check_file: list[Path] = []
    quota_command: str = ""
    stall_command: str = ""
    health_command: str = ""
    low_percent: float = LOW_PERCENT_DEFAULT
    report_file: Path | None = None
    watch: bool = False
    interval_s: int = 300
    max_iterations: int = 1
    run_rotation_plan: bool = False
    rotation_helper: Path = DEFAULT_ROTATION_HELPER


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    _ = parser.add_argument("--current-name", default="midas-team")
    _ = parser.add_argument("--candidate-name", default="")
    _ = parser.add_argument("--all-candidates", action="store_true")
    _ = parser.add_argument(
        "--propose-candidate",
        action="store_true",
        help="Rank candidates from redacted quota/smoke observations or report Docker-probe blockers.",
    )
    _ = parser.add_argument(
        "--candidate-observation-file",
        type=Path,
        action="append",
        default=[],
        help="Redacted per-candidate quota/smoke evidence; never raw credentials.",
    )
    _ = parser.add_argument("--min-high-percent", type=float, default=50.0)
    _ = parser.add_argument("--check-file", type=Path, action="append", default=[])
    _ = parser.add_argument("--quota-command", default="", help="Optional read-only quota command; advisory only, e.g. an OpenCode /quota wrapper.")
    _ = parser.add_argument("--stall-command", default="", help="Optional local no-model stall command.")
    _ = parser.add_argument("--health-command", default="", help="Optional local no-model health command, e.g. omo_manager_watchdog.sh.")
    _ = parser.add_argument("--low-percent", type=float, default=LOW_PERCENT_DEFAULT)
    _ = parser.add_argument("--report-file", type=Path)
    _ = parser.add_argument("--watch", action="store_true")
    _ = parser.add_argument("--interval-s", type=int, default=300)
    _ = parser.add_argument("--max-iterations", type=int, default=1)
    _ = parser.add_argument("--run-rotation-plan", action="store_true", help="Run metadata/smoke-mode plan only; never real smoke or live switch.")
    _ = parser.add_argument("--rotation-helper", type=Path, default=DEFAULT_ROTATION_HELPER)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.low_percent < 0 or parsed.low_percent > 100:
        parser.error("--low-percent must be between 0 and 100.")
    if parsed.min_high_percent < 0 or parsed.min_high_percent > 100:
        parser.error("--min-high-percent must be between 0 and 100.")
    if parsed.interval_s < 1 or parsed.interval_s > 86400:
        parser.error("--interval-s must be between 1 and 86400.")
    if parsed.max_iterations < 1 or parsed.max_iterations > 10000:
        parser.error("--max-iterations must be between 1 and 10000.")
    if parsed.candidate_name and Path(parsed.candidate_name).name != parsed.candidate_name:
        parser.error("--candidate-name must be a basename, not a path.")
    if Path(parsed.current_name).name != parsed.current_name:
        parser.error("--current-name must be a basename, not a path.")
    return Args(
        auth_dir=parsed.auth_dir.expanduser(),
        current_name=parsed.current_name,
        candidate_name=parsed.candidate_name,
        all_candidates=parsed.all_candidates,
        propose_candidate=parsed.propose_candidate,
        candidate_observation_file=tuple(path.expanduser() for path in parsed.candidate_observation_file),
        min_high_percent=parsed.min_high_percent,
        check_file=tuple(path.expanduser() for path in parsed.check_file),
        quota_command=parsed.quota_command,
        stall_command=parsed.stall_command,
        health_command=parsed.health_command,
        low_percent=parsed.low_percent,
        report_file=parsed.report_file.expanduser() if parsed.report_file else None,
        watch=parsed.watch,
        interval_s=parsed.interval_s,
        max_iterations=parsed.max_iterations,
        run_rotation_plan=parsed.run_rotation_plan,
        rotation_helper=parsed.rotation_helper.expanduser(),
    )



@dataclass(frozen=True)
class CandidateEvidence:
    name: str
    remaining_percent: float | None
    smoke_ok: bool | None
    source: str


def safe_candidate_files(auth_dir: Path) -> list[str]:
    try:
        return sorted(
            path.name
            for path in auth_dir.glob("auth*.json")
            if path.is_file() and not path.is_symlink() and path.name != "auth.json"
        )
    except OSError:
        return []


CANDIDATE_RE = re.compile(r"(?i)\b(?:candidate|credential|profile|auth(?:[_-]?file)?)\s*[:=]\s*([A-Za-z0-9_.-]+)")
PERCENT_RE = re.compile(r"(?i)\b(?:remaining|left|available|quota)\b[^\n%]{0,80}?([0-9]+(?:\.[0-9]+)?)\s*%")
SMOKE_OK_RE = re.compile(r"(?i)\b(?:smoke(?:_ok|_matched_expected)?|probe(?:_ok)?)\s*[:=]\s*(?:true|pass|passed|ok|yes)\b")
SMOKE_FAIL_RE = re.compile(r"(?i)\b(?:smoke(?:_ok|_matched_expected)?|probe(?:_ok)?)\s*[:=]\s*(?:false|fail|failed|no)\b")


def parse_candidate_evidence(path: Path) -> list[CandidateEvidence]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    evidence: list[CandidateEvidence] = []
    for line in text.splitlines():
        name_match = CANDIDATE_RE.search(line)
        if name_match is None:
            continue
        name = Path(name_match.group(1)).name
        percent_match = PERCENT_RE.search(line)
        remaining = float(percent_match.group(1)) if percent_match else None
        smoke_ok: bool | None = None
        if SMOKE_OK_RE.search(line):
            smoke_ok = True
        elif SMOKE_FAIL_RE.search(line) or EXHAUSTION_RE.search(line):
            smoke_ok = False
        evidence.append(CandidateEvidence(name, remaining, smoke_ok, str(path)))
    return evidence


def candidate_rotation_plan_command(args: Args, candidate: str) -> str:
    cmd = [
        sys.executable,
        str(args.rotation_helper),
        "--auth-dir",
        str(args.auth_dir),
        "--current-name",
        args.current_name,
        "--candidate-name",
        candidate,
        "--smoke-mode",
        "plan",
    ]
    return " ".join(shlex.quote(part) for part in cmd)


def render_candidate_proposal(args: Args) -> list[str]:
    if not args.propose_candidate:
        return []
    evidence: list[CandidateEvidence] = []
    for path in args.candidate_observation_file:
        evidence.extend(parse_candidate_evidence(path))
    candidates = safe_candidate_files(args.auth_dir)
    lines = [
        "candidate_quota_proposal:",
        "  docker_probe_policy: isolated_container_only; no_live_auth_mutation; no_secret_logging",
        "  exact_quota_reliability: advisory; combine quota evidence with deterministic smoke/error observations",
    ]
    viable = [item for item in evidence if item.smoke_ok is not False and item.remaining_percent is not None]
    viable.sort(key=lambda item: item.remaining_percent if item.remaining_percent is not None else -1, reverse=True)
    if viable:
        best = viable[0]
        best_remaining = best.remaining_percent if best.remaining_percent is not None else -1.0
        confidence = "high" if best_remaining >= args.min_high_percent and best.smoke_ok is True else "medium"
        lines.extend(
            [
                "  proposal_status: proposed_from_observations",
                f"  proposed_candidate: {best.name}",
                f"  observed_remaining_percent: {best.remaining_percent:g}",
                f"  smoke_observed: {str(best.smoke_ok).lower() if best.smoke_ok is not None else 'unknown'}",
                f"  confidence: {confidence}",
                "  required_human_action: approve exact candidate, rollback backup, real isolated Docker smoke if not already human-authorized, then live switch",
            ]
        )
    else:
        lines.extend(
            [
                "  proposal_status: blocked_no_reliable_candidate_quota_observation",
                "  blocker: need human-authorized isolated Docker quota/smoke probe per candidate; current task did not authorize real credential probes",
                "  safe_next_step: run metadata/smoke plans now; run real Docker probes only with explicit human authorization",
            ]
        )
    if candidates:
        lines.append("  candidate_plan_commands:")
        for name in candidates:
            lines.append(f"  - {candidate_rotation_plan_command(args, name)}")
    else:
        lines.append("  candidate_plan_commands: []")
    lines.extend(
        [
            "  live_switch: blocked_without_explicit_human_authorization",
            "  logs: redacted; credential contents never printed",
        ]
    )
    return lines

def redact(text: str) -> str:
    redacted = AUTH_BEARER_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    redacted = BEARER_RE.sub(lambda match: f"{match.group(1)}<redacted>", redacted)
    redacted = SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}<redacted>{match.group(2)}", redacted)
    redacted = OPENAI_KEY_RE.sub("<redacted-openai-key>", redacted)
    redacted = JWTISH_RE.sub("<redacted-token>", redacted)
    redacted = LONG_OPAQUE_RE.sub("<redacted-opaque>", redacted)
    return redacted.replace("\r", "")


def snippet(text: str) -> str:
    clean = redact(text)
    if len(clean) <= MAX_SNIPPET_CHARS:
        return clean
    return clean[:MAX_SNIPPET_CHARS] + "\n<truncated>"


def classify_text(label: str, text: str, low_percent: float) -> tuple[str, str]:
    if STALL_RE.search(text):
        return "stall", f"{label}: stall_or_no_progress_signal"
    if EXHAUSTION_RE.search(text):
        return "low", f"{label}: exhaustion keyword"
    lows: list[float] = []
    for match in LOW_REMAINING_RE.finditer(text):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if value <= low_percent:
            lows.append(value)
    if lows:
        return "low", f"{label}: remaining_percent<={low_percent:g}"
    if text.strip():
        return "ok_or_unknown", f"{label}: no low-quota signal"
    return "unknown", f"{label}: empty"


def read_sources(args: Args) -> list[tuple[str, str, str]]:
    sources: list[tuple[str, str, str]] = []
    for path in args.check_file:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            status, reason = classify_text(str(path), text, args.low_percent)
            sources.append((status, reason, snippet(text)))
        except OSError as exc:
            sources.append(("unknown", f"{path}: read_error:{type(exc).__name__}", ""))
    command_sources = (
        ("quota_command", args.quota_command, True),
        ("stall_command", args.stall_command, False),
        ("health_command", args.health_command, False),
    )
    for label, command, advisory_quota in command_sources:
        if not command:
            continue
        try:
            result = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
            status, reason = classify_text(label, combined, args.low_percent)
            if result.returncode != 0 and status not in {"low", "stall"}:
                if advisory_quota:
                    status = "unknown"
                    reason = f"{label}: exit={result.returncode}"
                else:
                    status = "stall"
                    reason = f"{label}: nonzero_exit={result.returncode}"
            sources.append((status, reason, snippet(combined)))
        except (OSError, subprocess.SubprocessError) as exc:
            status = "unknown" if advisory_quota else "stall"
            sources.append((status, f"{label}: error:{type(exc).__name__}", ""))
    if not sources:
        sources.append(("unknown", "no quota source configured", ""))
    return sources


def rotation_plan_command(args: Args) -> str:
    cmd = [
        sys.executable,
        str(args.rotation_helper),
        "--auth-dir",
        str(args.auth_dir),
        "--current-name",
        args.current_name,
    ]
    if args.all_candidates:
        cmd.append("--all-candidates")
    if args.candidate_name:
        cmd.extend(["--candidate-name", args.candidate_name, "--smoke-mode", "plan"])
    return " ".join(shlex.quote(part) for part in cmd)


def maybe_run_rotation_plan(args: Args) -> tuple[int, str]:
    if not args.run_rotation_plan:
        return 0, ""
    cmd = [
        sys.executable,
        str(args.rotation_helper),
        "--auth-dir",
        str(args.auth_dir),
        "--current-name",
        args.current_name,
    ]
    if args.all_candidates:
        cmd.append("--all-candidates")
    if args.candidate_name:
        cmd.extend(["--candidate-name", args.candidate_name, "--smoke-mode", "plan"])
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=False)
    return result.returncode, snippet("\n".join(part for part in (result.stdout, result.stderr) if part))


def render_report(args: Args, sources: list[tuple[str, str, str]], plan_rc: int, plan_text: str) -> str:
    low = any(status == "low" for status, _, _ in sources)
    stalled = any(status == "stall" for status, _, _ in sources)
    unknown = any(status == "unknown" for status, _, _ in sources)
    if stalled:
        status = "hang_or_stall_detected"
    elif low:
        status = "low_quota_detected"
    elif unknown:
        status = "quota_or_stall_unknown"
    else:
        status = "quota_not_low_no_stall"
    lines = [
        f"watcher_status: {status}",
        "watcher_running: false",
        "quota_report_advisory_only: true",
        "non_openai_trigger_path: local_files_commands_health_and_tmux_session_signals",
        "safe_automatic_action: notify_and_prepare_human_gated_plan_only",
        "live_credential_mutation: blocked_without_explicit_human_authorization",
        "real_smoke: blocked_without_human_authorized_smoke",
        f"current_name: {args.current_name}",
        f"candidate_name: {args.candidate_name or '<unset>'}",
        f"all_candidates: {str(args.all_candidates).lower()}",
        "quota_sources:",
    ]
    for status_item, reason, text in sources:
        lines.append(f"- status={status_item} reason={reason}")
        if status_item in {"low", "stall"} and text:
            lines.append("  redacted_signal: |-")
            for line in text.splitlines()[:8]:
                lines.append(f"    {line}")
    lines.extend(
        [
            "switch_initiation:",
            f"  rotation_plan_command: {rotation_plan_command(args)}",
            "  next_safe_steps: metadata preflight, candidate smoke plan, notify human, preserve rollback backup, await exact approval before real smoke or live switch",
            "  rollback_required_before_live_switch: true",
            "  pre_post_notification_required: true",
        ]
    )
    lines.extend(render_candidate_proposal(args))
    if args.run_rotation_plan:
        lines.append(f"rotation_plan_exit: {plan_rc}")
        if plan_text:
            lines.append("rotation_plan_redacted_output: |-")
            for line in plan_text.splitlines():
                lines.append(f"  {line}")
    return "\n".join(lines) + "\n"


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    _ = tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    _ = tmp.replace(path)


def run_once(args: Args) -> int:
    sources = read_sources(args)
    plan_rc, plan_text = maybe_run_rotation_plan(args)
    report = render_report(args, sources, plan_rc, plan_text)
    if args.report_file is not None:
        write_report(args.report_file, report)
    print(report, end="")
    return 1 if any(status in {"low", "stall"} for status, _, _ in sources) else 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    rc = 0
    iterations = args.max_iterations if args.watch else 1
    for idx in range(iterations):
        rc = run_once(args)
        if not args.watch or idx == iterations - 1:
            break
        time.sleep(args.interval_s)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
