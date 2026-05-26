"""Human-gated, rollback-safe OpenCode auth switch helper.

Dry-run by default. Execution requires both --execute and
--human-authorized-switch. It copies credentials atomically, writes only metadata
and private rollback backups, and never prints credential contents.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AUTH_DIR = Path.home() / ".local" / "share" / "opencode"
DEFAULT_BACKUP_ROOT = Path.home() / ".local" / "state" / "omo-manager" / "opencode-auth-switch"
DEFAULT_ROTATION_HELPER = Path.home() / ".config" / "omo_manager" / "opencode_auth_rotation_dryrun.py"


@dataclass(frozen=True)
class Args:
    auth_dir: Path
    current_name: str
    candidate_name: str
    backup_root: Path
    rotation_helper: Path
    execute: bool
    human_authorized_switch: bool
    validate: bool
    human_authorized_smoke: bool
    smoke_timeout_s: int


class ParsedArgs(argparse.Namespace):
    auth_dir: Path = DEFAULT_AUTH_DIR
    current_name: str = "midas-team"
    candidate_name: str = ""
    backup_root: Path = DEFAULT_BACKUP_ROOT
    rotation_helper: Path = DEFAULT_ROTATION_HELPER
    execute: bool = False
    human_authorized_switch: bool = False
    validate: bool = False
    human_authorized_smoke: bool = False
    smoke_timeout_s: int = 90


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    _ = parser.add_argument("--current-name", default="midas-team")
    _ = parser.add_argument("--candidate-name", required=True)
    _ = parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    _ = parser.add_argument("--rotation-helper", type=Path, default=DEFAULT_ROTATION_HELPER)
    _ = parser.add_argument("--execute", action="store_true")
    _ = parser.add_argument("--human-authorized-switch", action="store_true")
    _ = parser.add_argument("--validate", action="store_true")
    _ = parser.add_argument("--human-authorized-smoke", action="store_true")
    _ = parser.add_argument("--smoke-timeout-s", type=int, default=90)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    for label, name in (("--current-name", parsed.current_name), ("--candidate-name", parsed.candidate_name)):
        if Path(name).name != name:
            parser.error(f"{label} must be a basename, not a path.")
    if parsed.smoke_timeout_s < 1 or parsed.smoke_timeout_s > 600:
        parser.error("--smoke-timeout-s must be between 1 and 600.")
    return Args(
        auth_dir=parsed.auth_dir.expanduser(),
        current_name=parsed.current_name,
        candidate_name=parsed.candidate_name,
        backup_root=parsed.backup_root.expanduser(),
        rotation_helper=parsed.rotation_helper.expanduser(),
        execute=parsed.execute,
        human_authorized_switch=parsed.human_authorized_switch,
        validate=parsed.validate,
        human_authorized_smoke=parsed.human_authorized_smoke,
        smoke_timeout_s=parsed.smoke_timeout_s,
    )


def named_auth_path(auth_dir: Path, name: str) -> Path | None:
    candidates = [auth_dir / name, auth_dir / f"auth.{name}.json", auth_dir / f"auth-{name}.json", auth_dir / f"{name}.json"]
    matches = [path for path in candidates if path.exists()]
    return matches[0] if len(matches) == 1 else None


def check_private_regular(label: str, path: Path) -> list[str]:
    errors: list[str] = []
    try:
        st = path.lstat()
    except OSError as exc:
        return [f"{label}: stat failed {type(exc).__name__}"]
    if stat.S_ISLNK(st.st_mode):
        errors.append(f"{label}: must not be symlink")
    if not stat.S_ISREG(st.st_mode):
        errors.append(f"{label}: must be regular file")
    if st.st_mode & 0o077:
        errors.append(f"{label}: permissions too broad")
    return errors


def atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.tmp")
    _ = shutil.copyfile(src, tmp)
    tmp.chmod(0o600)
    _ = tmp.replace(dst)


def validate_candidate(args: Args) -> bool:
    if not args.validate:
        return True
    command = [
        sys.executable,
        str(args.rotation_helper),
        "--auth-dir",
        str(args.auth_dir),
        "--current-name",
        args.current_name,
        "--candidate-name",
        args.candidate_name,
        "--smoke-mode",
        "run",
        "--smoke-timeout-s",
        str(args.smoke_timeout_s),
    ]
    if args.human_authorized_smoke:
        command.append("--human-authorized-smoke")
    result = subprocess.run(command, text=True, capture_output=True, timeout=args.smoke_timeout_s + 60, check=False)
    text = result.stdout + "\n" + result.stderr
    for line in text.splitlines():
        if line.startswith(("metadata_ready:", "smoke_exit_code:", "smoke_matched_expected:", "smoke_error:", "refreshed_credential_produced:")):
            print(line)
    return "smoke_matched_expected: true" in text


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    auth_dir = args.auth_dir.resolve()
    live = auth_dir / "auth.json"
    candidate = named_auth_path(auth_dir, args.candidate_name)
    current = named_auth_path(auth_dir, args.current_name)
    errors: list[str] = []
    if candidate is None:
        errors.append("candidate: unique named auth file not found")
    if current is None:
        errors.append("current: unique named auth file not found")
    errors.extend(check_private_regular("live", live))
    if candidate is not None:
        errors.extend(check_private_regular("candidate", candidate))
    if current is not None:
        errors.extend(check_private_regular("current", current))
    if candidate is not None and candidate.resolve() == live.resolve():
        errors.append("candidate: must not be live auth.json")
    if candidate is not None and current is not None and candidate.resolve() == current.resolve():
        errors.append("candidate: must differ from current")
    print(f"switch_candidate: {args.candidate_name}")
    print(f"execute: {str(args.execute).lower()}")
    print(f"human_authorized_switch: {str(args.human_authorized_switch).lower()}")
    if errors:
        for error in errors:
            print(f"readiness_error: {error}")
        return 2
    if not args.execute:
        print("switch_status: planned_noop")
        print("live_mutation: false")
        return 0
    if not args.human_authorized_switch:
        print("switch_status: blocked_without_human_authorized_switch")
        return 2
    assert candidate is not None
    args.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.backup_root.chmod(0o700)
    backup_dir = args.backup_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.candidate_name}"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "auth.pre-switch.json"
    atomic_copy(live, backup)
    matched_snapshot = "none"
    if current is not None and filecmp.cmp(live, current, shallow=False):
        atomic_copy(live, current)
        matched_snapshot = current.name
    atomic_copy(candidate, live)
    metadata = backup_dir / "switch-metadata.txt"
    _ = metadata.write_text(
        f"candidate={args.candidate_name}\nbackup={backup}\nmatched_snapshot={matched_snapshot}\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    print(f"backup_dir: {backup_dir}")
    print(f"matched_snapshot: {matched_snapshot}")
    print("switch_status: switched")
    if validate_candidate(args):
        print("post_switch_validation: passed")
        print("rollback: not-needed")
        return 0
    atomic_copy(backup, live)
    print("post_switch_validation: failed")
    print("rollback: performed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
