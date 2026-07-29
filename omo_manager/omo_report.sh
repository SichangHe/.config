#!/usr/bin/env bash
set -euo pipefail
case "$0" in
  /proc/self/fd/*)
    if [ "${OMO_REPORT_IMMUTABLE_EXEC:-}" != "1" ]; then
      echo "immutable report-helper execution identity is missing" >&2
      exit 2
    fi
    ;;
  *)
    exec python3 -I -S - "$0" "$@" <<'PY'
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path

MAX_HELPER_BYTES = 4 * 1024 * 1024
source = Path(sys.argv[1]).resolve(strict=True)
fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
try:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > MAX_HELPER_BYTES:
        raise RuntimeError("report helper is not a safe owned regular file")
    chunks: list[bytes] = []
    remaining = MAX_HELPER_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    after = os.fstat(fd)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or len(payload) != before.st_size
        or len(payload) > MAX_HELPER_BYTES
    ):
        raise RuntimeError("report helper changed while creating its execution snapshot")
finally:
    os.close(fd)

read_fd, write_fd = os.pipe()
writer = os.fork()
if writer == 0:
    os.close(read_fd)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(write_fd, payload[offset:])
            if written <= 0:
                os._exit(2)
            offset += written
    finally:
        os.close(write_fd)
    os._exit(0)
os.close(write_fd)
os.set_inheritable(read_fd, True)
bash = shutil.which("bash")
if bash is None:
    raise RuntimeError("bash is required")
environment = dict(os.environ)
environment.update(
    {
        "OMO_REPORT_HELPER_PATH": str(source),
        "OMO_REPORT_HELPER_SHA256": hashlib.sha256(payload).hexdigest(),
        "OMO_REPORT_IMMUTABLE_EXEC": "1",
    }
)
os.execve(bash, [bash, f"/proc/self/fd/{read_fd}", *sys.argv[2:]], environment)
PY
    ;;
esac
readonly OMO_REPORT_HELPER_PATH OMO_REPORT_HELPER_SHA256 OMO_REPORT_IMMUTABLE_EXEC
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
env_root="${OMO_WORK_LOGS_ROOT:-}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_target="${OMO_MANAGER_TMUX_TARGET:-}"
if [ -n "$env_root" ]; then root="$env_root"; fi
task_file=""
producer_target=""
status=""
message_file=""
alloc_message_file=0
describe=0
agent="${OMO_AGENT_NAME:-agent}"
usage() {
  printf '%s\n' \
    "Usage: omo_report.sh --status STATUS --message-file FILE [--agent NAME]" \
    "       omo_report.sh --describe --status STATUS --message-file FILE [--agent NAME]" \
    "       omo_report.sh --alloc-message-file" \
    "" \
    "Allocate a private task-specific draft first, write the report through an editor or other non-shell text channel, then submit it with --status blocked|in-progress|done and --message-file." \
    "The helper infers routing from the producer pane; do not pass task-file, root, manager-target, or other manual route flags." \
    "A file named REPORT is refused unless it is in a private owner-only directory. Use --describe to validate and resolve a submission without recording it."
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status|--message-file|--agent)
      if [ "$#" -lt 2 ]; then echo "missing value for $1" >&2; usage >&2; exit 2; fi
      option="$1"
      value="$2"
      case "$option" in
        --status) status="$value" ;;
        --message-file) message_file="$value" ;;
        --agent) agent="$value" ;;
      esac
      shift 2
      ;;
    --alloc-message-file) alloc_message_file=1; shift ;;
    --describe) describe=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ "$alloc_message_file" -eq 1 ] && [ -n "$message_file" ]; then echo "--alloc-message-file cannot be combined with --message-file" >&2; exit 2; fi
if [ "$alloc_message_file" -eq 1 ] && [ "$describe" -eq 1 ]; then echo "--alloc-message-file cannot be combined with --describe" >&2; exit 2; fi
if [ "$alloc_message_file" -eq 0 ] && { [ -z "$status" ] || [ -z "$message_file" ]; }; then usage >&2; exit 2; fi
root_real=$(python3 -I -S -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$root")
task_root_real="$root_real"
if [ -z "$task_file" ]; then
  inferred_task=$(python3 -I -S - "$root_real" "$HOME/work_logs" <<'PY'
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
roots: list[Path] = []
for root_arg in sys.argv[1:]:
    root = Path(root_arg).resolve()
    if root not in roots:
        roots.append(root)
TASK_SECTIONS = {"current", "human pending", "low priority", "previous"}
ACTIVE_TASK_STATUSES = {"running", "long_running", "blocked"}
RUNNING_TASK_STATUSES = {"running", "long_running"}
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
TARGET_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?![A-Za-z0-9_.-])")
MAX_ROUTE_FILE_BYTES = 64 * 1024 * 1024
route_evidence: dict[str, dict[str, object]] = {}

def route_text(path: Path) -> str | None:
    path = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        record: dict[str, object] = {"exists": False, "path": str(path)}
        previous = route_evidence.setdefault(str(path), record)
        if previous != record:
            raise RuntimeError(f"routing evidence changed while resolving: {path}")
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot read routing evidence: {path}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > MAX_ROUTE_FILE_BYTES:
            raise RuntimeError(f"routing evidence is not a safe owned regular file: {path}")
        payload = b""
        while len(payload) <= MAX_ROUTE_FILE_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_ROUTE_FILE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity_before != identity_after or len(payload) != before.st_size or len(payload) > MAX_ROUTE_FILE_BYTES:
            raise RuntimeError(f"routing evidence changed while resolving: {path}")
    finally:
        os.close(fd)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"routing evidence is not UTF-8: {path}") from exc
    record = {
        "exists": True,
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    previous = route_evidence.setdefault(str(path), record)
    if previous != record:
        raise RuntimeError(f"routing evidence changed while resolving: {path}")
    return text

def evidence_json() -> str:
    return json.dumps(list(route_evidence.values()), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

def canonical_tmux_target(target: str) -> tuple[str, int, int] | None:
    if TARGET_RE.fullmatch(target) is None:
        return None
    session, rest = target.split(":", 1)
    window, dot, pane = rest.partition(".")
    return session, int(window), int(pane) if dot else 0

def same_tmux_target(left: str, right: str) -> bool:
    left_target = canonical_tmux_target(left)
    return left_target is not None and left_target == canonical_tmux_target(right)

def current_tmux_target() -> str:
    if shutil.which("tmux") is None:
        return ""
    pane = os.environ.get("TMUX_PANE", "").strip()
    command = ["tmux", "display-message", "-p", "#{session_name}\t#{window_index}\t#{pane_index}"]
    if pane:
        command[3:3] = ["-t", pane]
    elif not os.environ.get("TMUX"):
        return ""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    session, window, pane_index = (result.stdout.rstrip("\n").split("\t") + ["", "", ""])[:3]
    if not session or not window or not pane_index:
        return ""
    return f"{session}:{window}.{pane_index}"

def parse_frontmatter(path: Path) -> dict[str, str] | None:
    text = route_text(path)
    if text is None:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return None
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line or line.startswith("  - "):
            continue
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values

def task_refs(root: Path, sections: set[str]) -> list[tuple[Path, tuple[str, ...]]]:
    todo = root / "TODO.md"
    text = route_text(todo)
    if text is None:
        return []
    lines = text.splitlines()
    refs: list[tuple[Path, tuple[str, ...]]] = []
    seen: set[Path] = set()
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":") and stripped[:-1] in TASK_SECTIONS:
            section = stripped[:-1]
            continue
        if section not in sections:
            continue
        listed_targets = tuple(TARGET_TOKEN_RE.findall(line))
        for match in re.findall(r"`?([A-Za-z0-9_./-]+\.md)`?", line):
            path = (root / match).resolve(strict=False)
            if path in seen or path.name == "TODO.md":
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            seen.add(path)
            refs.append((path, listed_targets))
    return refs

current = current_tmux_target()
if not current:
    print("current tmux pane/window could not be identified; cannot infer report task", file=sys.stderr)
    raise SystemExit(2)
for root in roots:
    matches: list[Path] = []
    running_matches: list[Path] = []
    for candidate, listed_targets in task_refs(root, TASK_SECTIONS):
        if listed_targets and not any(same_tmux_target(target, current) for target in listed_targets):
            continue
        metadata = parse_frontmatter(candidate)
        status = metadata.get("status") if metadata is not None else None
        if status not in ACTIVE_TASK_STATUSES:
            continue
        runat = metadata.get("runat", "")
        if TARGET_RE.fullmatch(runat) and same_tmux_target(runat, current):
            matches.append(candidate)
            if status in RUNNING_TASK_STATUSES:
                running_matches.append(candidate)
    if len(matches) == 1:
        print(f"{root}\t{matches[0].relative_to(root)}\t{current}\t{evidence_json()}")
        raise SystemExit(0)
    if len(matches) > 1:
        if len(running_matches) == 1:
            print(f"{root}\t{running_matches[0].relative_to(root)}\t{current}\t{evidence_json()}")
            raise SystemExit(0)
        current_refs = {candidate for candidate, _ in task_refs(root, {"current"})}
        current_matches = [candidate for candidate in matches if candidate in current_refs]
        if len(current_matches) == 1:
            print(f"{root}\t{current_matches[0].relative_to(root)}\t{current}\t{evidence_json()}")
            raise SystemExit(0)
        choices = ", ".join(str(path.relative_to(root)) for path in matches)
        print(f"multiple active task files match tmux target {current}: {choices}", file=sys.stderr)
        raise SystemExit(2)
print(f"could not infer task file for tmux target {current}", file=sys.stderr)
raise SystemExit(2)
PY
  )
  IFS=$'\t' read -r task_root_real task_file producer_target task_route_evidence <<EOF
$inferred_task
EOF
fi
path_real=$(python3 -I -S -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]) / sys.argv[2]).resolve(strict=False))' "$task_root_real" "$task_file")
case "$path_real" in "$task_root_real"/*) ;; *) echo "task file escapes root" >&2; exit 2 ;; esac
if [ "$alloc_message_file" -eq 1 ]; then
  python3 -I -S - "$path_real" <<'PY'
from __future__ import annotations
import os
import re
import sys
import tempfile
from pathlib import Path
task_path = Path(sys.argv[1])
safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", task_path.stem).strip("._-")[:80] or "task"
drafts_dir = Path("/tmp") / f"omo-report-drafts-{os.getuid()}"
drafts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
drafts_dir.chmod(0o700)
fd, tmp_name = tempfile.mkstemp(prefix=f"{safe_stem}.", suffix=".md", dir=drafts_dir)
os.close(fd)
path = Path(tmp_name)
path.chmod(0o600)
print(path)
PY
  exit 0
fi
python3 -I -S - "$message_file" <<'PY'
from __future__ import annotations
import os
import stat
import sys
from pathlib import Path
raw_path = Path(sys.argv[1])
path = raw_path.resolve(strict=False)
if raw_path.name != "REPORT":
    raise SystemExit(0)
try:
    parent_stat = path.parent.stat()
except FileNotFoundError:
    parent_stat = None
if parent_stat is not None and parent_stat.st_uid == os.getuid() and stat.S_IMODE(parent_stat.st_mode) & 0o077 == 0:
    raise SystemExit(0)
print(f"refusing shared report path: {sys.argv[1]}", file=sys.stderr)
print("files named REPORT are accepted only from a private owner-only directory", file=sys.stderr)
print("allocate a private path first: omo_report.sh --alloc-message-file", file=sys.stderr)
raise SystemExit(2)
PY
if [ ! -f "$message_file" ]; then echo "message file not found" >&2; exit 2; fi
if [ ! -f "$path_real" ]; then echo "task file not found" >&2; exit 2; fi
append_info=$(python3 -I -S - "$task_root_real" "$path_real" "$manager_target" <<'PY'
from __future__ import annotations
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
root = Path(sys.argv[1])
task_path = Path(sys.argv[2])
main_target = sys.argv[3].strip()
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
TARGET_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?![A-Za-z0-9_.-])")
TASK_SECTIONS = {"current", "human pending", "low priority", "previous"}
ACTIVE_MANAGER_STATUSES = {"running", "long_running", "blocked"}
RUNNING_MANAGER_STATUSES = {"running", "long_running"}
MAX_ROUTE_FILE_BYTES = 64 * 1024 * 1024
route_evidence: dict[str, dict[str, object]] = {}
route_local_date = datetime.now().astimezone().strftime("%Y-%m-%d")

def route_text(path: Path) -> str | None:
    path = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        record: dict[str, object] = {"exists": False, "path": str(path)}
        previous = route_evidence.setdefault(str(path), record)
        if previous != record:
            raise RuntimeError(f"routing evidence changed while resolving: {path}")
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot read routing evidence: {path}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > MAX_ROUTE_FILE_BYTES:
            raise RuntimeError(f"routing evidence is not a safe owned regular file: {path}")
        payload = b""
        while len(payload) <= MAX_ROUTE_FILE_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_ROUTE_FILE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity_before != identity_after or len(payload) != before.st_size or len(payload) > MAX_ROUTE_FILE_BYTES:
            raise RuntimeError(f"routing evidence changed while resolving: {path}")
    finally:
        os.close(fd)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"routing evidence is not UTF-8: {path}") from exc
    record = {
        "exists": True,
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    previous = route_evidence.setdefault(str(path), record)
    if previous != record:
        raise RuntimeError(f"routing evidence changed while resolving: {path}")
    return text

def evidence_json() -> str:
    return json.dumps(list(route_evidence.values()), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

def canonical_tmux_target(target: str) -> tuple[str, int, int] | None:
    if TARGET_RE.fullmatch(target) is None:
        return None
    session, rest = target.split(":", 1)
    window, dot, pane = rest.partition(".")
    return session, int(window), int(pane) if dot else 0

def same_tmux_target(left: str, right: str) -> bool:
    left_target = canonical_tmux_target(left)
    return left_target is not None and left_target == canonical_tmux_target(right)

def target_session(target: str) -> str:
    return target.split(":", 1)[0] if ":" in target else ""

def is_named_main_manager_target(target: str) -> bool:
    return target_session(target) in {"main", "omo-manager"}

def parse_frontmatter(path: Path) -> dict[str, str] | None:
    text = route_text(path)
    if text is None:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return None
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line or line.startswith("  - "):
            continue
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values

def main_manager_file() -> Path:
    return root / f"work_manager_{route_local_date}.md"

def print_route(path: Path, note: str, requested: str, resolved: str, kind: str) -> None:
    print(path)
    print(note)
    print(requested)
    print(resolved)
    print(kind)
    print(evidence_json())
    print(route_local_date)

def active_task_refs() -> list[tuple[Path, tuple[str, ...]]]:
    todo = root / "TODO.md"
    text = route_text(todo)
    if text is None:
        return []
    lines = text.splitlines()
    refs: list[tuple[Path, tuple[str, ...]]] = []
    seen: set[Path] = set()
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":") and stripped[:-1] in TASK_SECTIONS:
            section = stripped[:-1]
            continue
        if section not in TASK_SECTIONS:
            continue
        listed_targets = tuple(TARGET_TOKEN_RE.findall(line))
        for match in re.findall(r"`?([A-Za-z0-9_./-]+\.md)`?", line):
            path = (root / match).resolve(strict=False)
            if path in seen or path.name == "TODO.md":
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            seen.add(path)
            refs.append((path, listed_targets))
    return refs

metadata = parse_frontmatter(task_path)
if metadata is None:
    if task_path.name == "work_manager.md":
        print_route(main_manager_file(), "", main_target, main_target, "legacy-main-manager")
        raise SystemExit(0)
    if task_path.name.startswith("work_manager_"):
        print_route(task_path, "", main_target, main_target, "main-manager")
        raise SystemExit(0)
    print("task frontmatter is required to route report", file=sys.stderr)
    raise SystemExit(2)
managerat = metadata.get("managerat", "")
if not TARGET_RE.fullmatch(managerat):
    print("task frontmatter `managerat` must be a tmux target", file=sys.stderr)
    raise SystemExit(2)
if same_tmux_target(managerat, main_target):
    print_route(main_manager_file(), "", managerat, main_target, "configured-main-manager")
    raise SystemExit(0)
if is_named_main_manager_target(managerat):
    print_route(main_manager_file(), "", managerat, main_target or managerat, "named-main-manager")
    raise SystemExit(0)
manager_matches: list[tuple[Path, str]] = []
running_manager_matches: list[tuple[Path, str]] = []
for candidate, listed_targets in active_task_refs():
    if listed_targets and not any(same_tmux_target(target, managerat) for target in listed_targets):
        continue
    candidate_metadata = parse_frontmatter(candidate)
    status = candidate_metadata.get("status") if candidate_metadata is not None else None
    if candidate_metadata is None or candidate_metadata.get("is_manager") != "true" or status not in ACTIVE_MANAGER_STATUSES:
        continue
    runat = candidate_metadata.get("runat", "")
    if same_tmux_target(runat, managerat):
        manager_matches.append((candidate, runat))
        if status in RUNNING_MANAGER_STATUSES:
            running_manager_matches.append((candidate, runat))
if len(manager_matches) == 1:
    candidate, runat = manager_matches[0]
    print_route(candidate, "", managerat, runat, "active-manager-task")
    raise SystemExit(0)
if len(manager_matches) > 1:
    if len(running_manager_matches) == 1:
        candidate, runat = running_manager_matches[0]
        print_route(candidate, "", managerat, runat, "active-manager-task")
        raise SystemExit(0)
    choices = ", ".join(str(candidate.relative_to(root)) for candidate, _ in manager_matches)
    print(f"multiple active manager task files match tmux target {managerat}: {choices}", file=sys.stderr)
    raise SystemExit(2)
note = f"Target manager `{managerat}` has no active manager task file. Main manager: find where that manager moved or reassign this report."
print_route(main_manager_file(), note, managerat, main_target, "main-manager-fallback")
PY
)
mapfile -t append_fields <<<"$append_info"
if [ "${#append_fields[@]}" -ne 7 ]; then echo "report route resolution returned incomplete evidence" >&2; exit 2; fi
append_path_real="${append_fields[0]}"
route_note="${append_fields[1]}"
requested_manager_target="${append_fields[2]}"
resolved_manager_target="${append_fields[3]}"
route_kind="${append_fields[4]}"
manager_route_evidence="${append_fields[5]}"
route_local_date="${append_fields[6]}"
case "$append_path_real" in "$task_root_real"/*) ;; *) echo "report route escapes root" >&2; exit 2 ;; esac
tmux_target="${TMUX_PANE:-}"
tmux_info=""
if command -v tmux >/dev/null 2>&1; then
  if [ -n "$tmux_target" ]; then
    tmux_info=$(tmux display-message -p -t "$tmux_target" '#{session_name}	#{window_index}	#{pane_index}	#{pane_id}	#{window_name}' 2>/dev/null || true)
  elif [ -n "${TMUX:-}" ]; then
    tmux_info=$(tmux display-message -p '#{session_name}	#{window_index}	#{pane_index}	#{pane_id}	#{window_name}' 2>/dev/null || true)
  fi
fi
tmux_session=""
tmux_window_index=""
tmux_pane_index=""
tmux_pane_id=""
tmux_window_name=""
if [ -n "$tmux_info" ]; then
  IFS=$'\t' read -r tmux_session tmux_window_index tmux_pane_index tmux_pane_id tmux_window_name <<EOF
$tmux_info
EOF
fi
helper_path="${OMO_REPORT_HELPER_PATH:?}"
receiver_path="$(dirname "$helper_path")/omo_report_receipt.py"
pending_digest_path="$(dirname "$helper_path")/omo_pending_digest.py"
task_lock_path="$(dirname "$helper_path")/omo_task_lock.py"
mode="submit"
if [ "$describe" -eq 1 ]; then mode="describe"; fi
exec env OMO_REPORT_RECEIVER_BOOTSTRAP=1 PYTHONDONTWRITEBYTECODE=1 python3 -I -S - "$receiver_path" "$pending_digest_path" "$task_lock_path" "$helper_path" \
  --mode "$mode" \
  --helper "$helper_path" \
  --root "$task_root_real" \
  --task "$path_real" \
  --manager "$append_path_real" \
  --requested-manager-target "$requested_manager_target" \
  --resolved-manager-target "$resolved_manager_target" \
  --route-kind "$route_kind" \
  --route-note "$route_note" \
  --task-route-evidence "$task_route_evidence" \
  --manager-route-evidence "$manager_route_evidence" \
  --route-local-date "$route_local_date" \
  --status "$status" \
  --message-file "$message_file" \
  --agent "$agent" \
  --producer-target "$producer_target" \
  --tmux-session "$tmux_session" \
  --tmux-window-index "$tmux_window_index" \
  --tmux-pane-index "$tmux_pane_index" \
  --tmux-pane-id "$tmux_pane_id" \
  --tmux-window-name "$tmux_window_name" <<'PY'
from __future__ import annotations

import hashlib
import os
import stat
import sys
import types
from pathlib import Path

MAX_SOURCE_BYTES = 4 * 1024 * 1024

def source_bytes(raw_path: str) -> tuple[Path, bytes]:
    path = Path(raw_path).resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > MAX_SOURCE_BYTES:
            raise RuntimeError(f"helper source is not a safe owned regular file: {path}")
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or len(payload) != before.st_size
            or len(payload) > MAX_SOURCE_BYTES
        ):
            raise RuntimeError(f"helper source changed while creating its execution snapshot: {path}")
    finally:
        os.close(fd)
    return path, payload

def load_module(name: str, path: Path, payload: bytes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = "omo_manager"
    module.__executed_source_sha256__ = hashlib.sha256(payload).hexdigest()
    sys.modules[name] = module
    exec(compile(payload, str(path), "exec"), module.__dict__)
    return module

receiver_path, receiver_payload = source_bytes(sys.argv[1])
pending_path, pending_payload = source_bytes(sys.argv[2])
lock_path, lock_payload = source_bytes(sys.argv[3])
helper_path = Path(sys.argv[4]).resolve(strict=True)
receiver_arguments = sys.argv[5:]
package = types.ModuleType("omo_manager")
package.__package__ = "omo_manager"
package.__path__ = []
sys.modules["omo_manager"] = package
load_module("omo_manager.omo_pending_digest", pending_path, pending_payload)
load_module("omo_manager.omo_task_lock", lock_path, lock_payload)
receiver = load_module("omo_manager.omo_report_receipt", receiver_path, receiver_payload)
receiver.__executed_helper_path__ = str(helper_path)
receiver.__executed_helper_sha256__ = os.environ.get("OMO_REPORT_HELPER_SHA256", "")
sys.argv = [str(receiver_path), *receiver_arguments]
raise SystemExit(receiver.main())
PY
