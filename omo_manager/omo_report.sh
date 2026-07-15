#!/usr/bin/env bash
set -euo pipefail
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
status=""
message_file=""
alloc_message_file=0
agent="${OMO_AGENT_NAME:-agent}"
usage() {
  printf '%s\n' \
    "Usage: omo_report.sh --status STATUS --message-file FILE [--agent NAME]" \
    "       omo_report.sh --alloc-message-file" \
    "Create report text in a private helper-allocated file, then pass that path with --message-file. A file named REPORT is refused unless it is in a private owner-only directory."
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status) status="$2"; shift 2 ;;
    --message-file) message_file="$2"; shift 2 ;;
    --alloc-message-file) alloc_message_file=1; shift ;;
    --agent) agent="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ "$alloc_message_file" -eq 1 ] && [ -n "$message_file" ]; then echo "--alloc-message-file cannot be combined with --message-file" >&2; exit 2; fi
if [ "$alloc_message_file" -eq 0 ] && { [ -z "$status" ] || [ -z "$message_file" ]; }; then usage >&2; exit 2; fi
root_real=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$root")
if [ -z "$task_file" ]; then
  task_file=$(python3 - "$root_real" <<'PY'
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
root = Path(sys.argv[1])
ACTIVE_SECTIONS = {"current", "human pending", "low priority"}
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")

def target_parts(target: str) -> tuple[str, str, str]:
    session, sep, rest = target.partition(":")
    if not sep:
        return "", "", ""
    window, dot, pane = rest.partition(".")
    return session, window, pane if dot else ""

def same_tmux_target(left: str, right: str) -> bool:
    left_session, left_window, left_pane = target_parts(left)
    right_session, right_window, right_pane = target_parts(right)
    if not left_session or not right_session:
        return False
    if left_session != right_session or left_window != right_window:
        return False
    return not left_pane or not right_pane or left_pane == right_pane

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
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
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

def task_refs(sections: set[str]) -> list[Path]:
    todo = root / "TODO.md"
    try:
        lines = todo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    refs: list[Path] = []
    seen: set[Path] = set()
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":") and stripped[:-1] in ACTIVE_SECTIONS | {"previous"}:
            section = stripped[:-1]
            continue
        if section not in sections:
            continue
        for match in re.findall(r"`?([A-Za-z0-9_./-]+\.md)`?", line):
            path = (root / match).resolve(strict=False)
            if path in seen or path.name == "TODO.md":
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            seen.add(path)
            refs.append(path)
    return refs

def active_task_refs() -> list[Path]:
    return task_refs(ACTIVE_SECTIONS)

current = current_tmux_target()
if not current:
    print("current tmux pane/window could not be identified; cannot infer report task", file=sys.stderr)
    raise SystemExit(2)
matches: list[Path] = []
for candidate in active_task_refs():
    metadata = parse_frontmatter(candidate)
    if metadata is None:
        continue
    runat = metadata.get("runat", "")
    if TARGET_RE.fullmatch(runat) and same_tmux_target(runat, current):
        matches.append(candidate)
if len(matches) == 1:
    print(matches[0].relative_to(root))
    raise SystemExit(0)
if len(matches) > 1:
    current_refs = set(task_refs({"current"}))
    current_matches = [candidate for candidate in matches if candidate in current_refs]
    if len(current_matches) == 1:
        print(current_matches[0].relative_to(root))
        raise SystemExit(0)
if not matches:
    print(f"could not infer task file for tmux target {current}", file=sys.stderr)
else:
    choices = ", ".join(str(path.relative_to(root)) for path in matches)
    print(f"multiple active task files match tmux target {current}: {choices}", file=sys.stderr)
raise SystemExit(2)
PY
  )
fi
path_real=$(python3 -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]) / sys.argv[2]).resolve(strict=False))' "$root_real" "$task_file")
case "$path_real" in "$root_real"/*) ;; *) echo "task file escapes root" >&2; exit 2 ;; esac
if [ "$alloc_message_file" -eq 1 ]; then
  python3 - "$path_real" <<'PY'
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
python3 - "$message_file" <<'PY'
from __future__ import annotations
import os
import stat
import sys
from pathlib import Path
path = Path(sys.argv[1]).resolve(strict=False)
if path.name != "REPORT":
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
append_info=$(python3 - "$root_real" "$path_real" "$manager_target" <<'PY'
from __future__ import annotations
import re
import sys
from datetime import datetime
from pathlib import Path
root = Path(sys.argv[1])
task_path = Path(sys.argv[2])
main_target = sys.argv[3].strip()
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
ACTIVE_SECTIONS = {"current", "human pending", "low priority"}
ACTIVE_MANAGER_STATUSES = {"running", "blocked"}

def target_parts(target: str) -> tuple[str, str, str]:
    session, sep, rest = target.partition(":")
    if not sep:
        return "", "", ""
    window, dot, pane = rest.partition(".")
    return session, window, pane if dot else ""

def same_tmux_target(left: str, right: str) -> bool:
    left_session, left_window, left_pane = target_parts(left)
    right_session, right_window, right_pane = target_parts(right)
    if not left_session or not right_session:
        return False
    if left_session != right_session or left_window != right_window:
        return False
    return not left_pane or not right_pane or left_pane == right_pane

def target_session(target: str) -> str:
    return target.split(":", 1)[0] if ":" in target else ""

def is_named_main_manager_target(target: str) -> bool:
    return target_session(target) in {"main", "omo-manager"}

def parse_frontmatter(path: Path) -> dict[str, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
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
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    return root / f"work_manager_{today}.md"

def print_route(path: Path, note: str = "") -> None:
    print(f"{path}\t{note}")

def active_task_refs() -> list[Path]:
    todo = root / "TODO.md"
    try:
        lines = todo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    refs: list[Path] = []
    seen: set[Path] = set()
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":") and stripped[:-1] in ACTIVE_SECTIONS | {"previous"}:
            section = stripped[:-1]
            continue
        if section not in ACTIVE_SECTIONS:
            continue
        for match in re.findall(r"`?([A-Za-z0-9_./-]+\.md)`?", line):
            path = (root / match).resolve(strict=False)
            if path in seen or path.name == "TODO.md":
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            seen.add(path)
            refs.append(path)
    return refs

metadata = parse_frontmatter(task_path)
if metadata is None:
    if task_path.name == "work_manager.md":
        print_route(main_manager_file())
        raise SystemExit(0)
    if task_path.name.startswith("work_manager_"):
        print_route(task_path)
        raise SystemExit(0)
    print("task frontmatter is required to route report", file=sys.stderr)
    raise SystemExit(2)
managerat = metadata.get("managerat", "")
if not TARGET_RE.fullmatch(managerat):
    print("task frontmatter `managerat` must be a tmux target", file=sys.stderr)
    raise SystemExit(2)
if same_tmux_target(managerat, main_target):
    print_route(main_manager_file())
    raise SystemExit(0)
if is_named_main_manager_target(managerat):
    print_route(main_manager_file())
    raise SystemExit(0)
for candidate in active_task_refs():
    candidate_metadata = parse_frontmatter(candidate)
    if (
        candidate_metadata is None
        or candidate_metadata.get("is_manager") != "true"
        or candidate_metadata.get("status") not in ACTIVE_MANAGER_STATUSES
    ):
        continue
    runat = candidate_metadata.get("runat", "")
    if same_tmux_target(runat, managerat):
        print_route(candidate)
        raise SystemExit(0)
note = f"Target manager `{managerat}` has no active manager task file. Main manager: find where that manager moved or reassign this report."
print_route(main_manager_file(), note)
PY
)
IFS=$'\t' read -r append_path_real route_note <<EOF
$append_info
EOF
mkdir -p "$(dirname "$append_path_real")"
stamp=$(date '+%H:%M')
append_kv() {
  python3 - "$1" "$2" <<'PY'
from __future__ import annotations
import sys
from urllib.parse import quote
key, value = sys.argv[1:3]
if value:
    print(f" {key}={quote(value, safe=':@._/-%')}", end="")
PY
}
old_legacy_source_line="(from agent ${agent} via omo_report.sh status=${status})"
old_source_prefix="[omo-message-source: origin=agent agent=${agent} via=omo_report.sh status=${status}"
old_source_line="${old_source_prefix}"
tmux_target="${TMUX_PANE:-}"
pointer_label="$agent"
tmux_info=""
if command -v tmux >/dev/null 2>&1; then
  if [ -n "$tmux_target" ]; then
    tmux_info=$(tmux display-message -p -t "$tmux_target" '#{session_name}	#{window_index}	#{pane_index}	#{pane_id}	#{window_name}' 2>/dev/null || true)
  elif [ -n "${TMUX:-}" ]; then
    tmux_info=$(tmux display-message -p '#{session_name}	#{window_index}	#{pane_index}	#{pane_id}	#{window_name}' 2>/dev/null || true)
  fi
fi
if [ -n "$tmux_info" ]; then
  IFS=$'\t' read -r tmux_session tmux_window_index tmux_pane_index tmux_pane_id tmux_window_name <<EOF
$tmux_info
EOF
  old_source_line="${old_source_line}$(append_kv tmux_session "$tmux_session")"
  old_source_line="${old_source_line}$(append_kv tmux_window_index "$tmux_window_index")"
  old_source_line="${old_source_line}$(append_kv tmux_pane_index "$tmux_pane_index")"
  old_source_line="${old_source_line}$(append_kv tmux_pane_id "$tmux_pane_id")"
  if [ -n "$tmux_session" ] && [ -n "$tmux_window_index" ] && [ -n "$tmux_pane_index" ]; then
    old_source_line="${old_source_line}$(append_kv tmux_target "${tmux_session}:${tmux_window_index}.${tmux_pane_index}")"
  fi
  if [ -n "$tmux_session" ] && [ -n "$tmux_window_index" ]; then
    pointer_label="${tmux_session}:${tmux_window_index}"
    if [ -n "$tmux_pane_index" ] && [ "$tmux_pane_index" != "0" ]; then
      pointer_label="${pointer_label}.${tmux_pane_index}"
    fi
  fi
  old_source_line="${old_source_line}$(append_kv tmux_window_name "$tmux_window_name")"
elif [ -n "$tmux_target" ]; then
  old_source_line="${old_source_line}$(append_kv tmux_pane_id "$tmux_target")"
  pointer_label="$tmux_target"
fi
old_source_line="${old_source_line}]"
old_legacy_source_match="$old_legacy_source_line"
if [ "$old_source_line" != "${old_source_prefix}]" ]; then
  old_legacy_source_match=""
fi
pointer_info=$(python3 - "$message_file" "$agent" "$status" "$stamp" "$path_real" "$pointer_label" "$route_note" <<'PY'
from __future__ import annotations
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
message_path = Path(sys.argv[1])
agent, status, stamp = sys.argv[2:5]
task_path = Path(sys.argv[5])
pointer_label = sys.argv[6]
route_note = sys.argv[7]
message_bytes = message_path.read_bytes()
message_hash = hashlib.sha256(message_bytes).hexdigest()

def safe_part(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return part[:80] or "unknown"

def safe_label(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9:._%-]+", "_", value.strip()).strip("._-")
    return part[:80] or "unknown"

label = safe_label(pointer_label)
task_basename = safe_label(task_path.name)
reports_dir = Path("/tmp") / f"omo-agent-messages-{os.getuid()}"
reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
reports_dir.chmod(0o700)
report_key_parts = [message_bytes, agent.encode(), status.encode(), label.encode(), str(task_path).encode()]
if route_note:
    report_key_parts.append(route_note.encode())
report_key = hashlib.sha256(b"\0".join(report_key_parts)).hexdigest()
report_path = reports_dir / f"{safe_part(agent)}_{safe_part(status)}_{report_key}.md"
pointer_line = f"(from agent {label} {report_path})"
sent_line = f"(sent from {agent} via omo_report.sh tmux={label} time={stamp} task-file={task_basename})"
header_lines = [
        sent_line,
        f"[message-sha256: {message_hash}]",
]
if route_note:
    header_lines.extend(["route-warning:", route_note])
header_lines.append("message:")
header = "\n".join(header_lines).encode("utf-8") + b"\n"
body = header + message_bytes
if report_path.exists():
    current = report_path.read_bytes()
    try:
        current_header, current_message = current.split(b"message:\n", 1)
        current_header_text = current_header.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"stale or corrupt report file: {report_path}") from exc
    stable_expected_lines = [
        f"[message-sha256: {message_hash}]",
    ]
    current_header_lines = current_header_text.splitlines()
    if current_message != message_bytes or any(line not in current_header_lines for line in stable_expected_lines):
        raise RuntimeError(f"stale or corrupt report file: {report_path}")
    if current != body:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{report_path.name}.", suffix=".tmp", dir=reports_dir)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        tmp_path.chmod(0o600)
        os.replace(tmp_path, report_path)
    report_path.chmod(0o600)
else:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{report_path.name}.", suffix=".tmp", dir=reports_dir)
    tmp_path = Path(tmp_name)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    tmp_path.chmod(0o600)
    os.replace(tmp_path, report_path)
    report_path.chmod(0o600)
print(f"{message_hash}\t{report_path}\t{pointer_line}")
PY
)
IFS=$'\t' read -r message_hash durable_message_file pointer_line <<EOF
$pointer_info
EOF
lock_path="${append_path_real}.omo_report.lock"
exec 9>"$lock_path"
flock 9
if [ ! -f "$append_path_real" ]; then : >"$append_path_real"; fi
python3 - "$append_path_real" "$message_hash" "$pointer_line" "$old_legacy_source_match" "$old_source_line" <<'PY'
from __future__ import annotations
import re
import sys
from pathlib import Path
path = Path(sys.argv[1])
message_hash, pointer_line, old_legacy_source_match, old_source_line = sys.argv[2:6]
hash_line = f"[message-sha256: {message_hash}]"
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

def without_old_report_file(line: str) -> str:
    line = re.sub(r" report_file=[^ \]]+", "", line)
    line = re.sub(r" report_sha256=[^ \]]+", "", line)
    line = re.sub(r" report-file=[^ )]+", "", line)
    return line

def without_volatile_tmux(line: str) -> str:
    line = re.sub(r" tmux_pane_id=[^ \]]+", "", line)
    line = re.sub(r" tmux_window_name=[^ \]]+", "", line)
    return line

for idx, line in enumerate(lines):
    if line.strip() != "(pending)":
        continue
    block = lines[idx:]
    for next_idx, block_line in enumerate(block[1:], start=idx + 1):
        if block_line.strip() == "(pending)":
            block = lines[idx:next_idx]
            break
    if any(block_line.strip().startswith(("(manager handled:", "(manager routed:")) for block_line in block[1:]):
        continue
    stripped_block = [block_line.strip() for block_line in block]
    normalized_block = [without_old_report_file(block_line) for block_line in stripped_block]
    stable_source_block = [without_volatile_tmux(block_line) for block_line in normalized_block]
    stable_old_source_line = without_volatile_tmux(old_source_line)
    if pointer_line in stripped_block:
        raise SystemExit(0)
    if hash_line in stripped_block and (
        stable_old_source_line in stable_source_block
        or (old_legacy_source_match and old_legacy_source_match in normalized_block)
    ):
        raise SystemExit(0)
block = [
    "",
    "(pending)",
    pointer_line,
]
path.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
PY
