#!/usr/bin/env python3
"""Durable non-urgent digest queue for manager-timed human delivery."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_STATE_DIR = Path(os.environ.get("OMO_MANAGER_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))
DEFAULT_QUEUE_FILE = Path("MANAGER_DIGEST_QUEUE.md")
DEFAULT_MAIL_DIR = os.environ.get("OMO_MANAGER_MAIL_DIR", "")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
DEFAULT_SEND_HELPER = Path.home() / ".config/omo_manager/omo_email_human.sh"
LOCAL_TZ = datetime.now().astimezone().tzinfo
BLOCK_RE = re.compile(r"(?ms)^---\n\(digest-item\)\n(?P<body>.*?)(?=^---\n\(digest-item\)\n|\Z)")


@dataclass(frozen=True)
class QueueItem:
    item_id: str
    status: str
    queued_at: str
    published_at: str
    source: str
    title: str
    url: str
    age: str
    summary: str
    raw: str


@dataclass(frozen=True)
class DeliveryDecision:
    eligible: bool
    reasons: list[str]


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone()


def display_time(value: str) -> str:
    parsed = parse_iso(value)
    if parsed is None:
        return safe_single_line(value)
    return parsed.strftime("%Y-%m-%d %H:%M %Z")


def published_at_from_relative_age(queued_at: str, age: str) -> str:
    match = re.search(r"\bPublished\s+(\d+)\s+(minute|minutes|hour|hours|day|days)\s+ago\b", age, re.I)
    queued = parse_iso(queued_at)
    if match is None or queued is None:
        return ""
    amount = int(match.group(1))
    unit = match.group(2).lower()
    delta = timedelta(minutes=amount) if unit.startswith("minute") else timedelta(hours=amount) if unit.startswith("hour") else timedelta(days=amount)
    return (queued - delta).isoformat(timespec="seconds")


def safe_single_line(value: str) -> str:
    return " ".join(value.split())


def quote_lines(text: str) -> list[str]:
    lines = text.splitlines() or [""]
    return [f"> {line}" for line in lines]


def unquote_lines(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        out.append(line[2:] if line.startswith("> ") else line)
    return "\n".join(out).strip()


def item_hash(source: str, title: str, url: str, summary: str) -> str:
    raw = "\n".join([source.strip(), title.strip(), url.strip(), summary.strip()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def parse_block(raw: str) -> QueueItem | None:
    fields: dict[str, str] = {}
    summary_lines: list[str] = []
    in_summary = False
    for line in raw.splitlines():
        if line == "summary:":
            in_summary = True
            continue
        if in_summary:
            summary_lines.append(line)
            continue
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    item_id = fields.get("id", "")
    if not item_id:
        return None
    return QueueItem(
        item_id=item_id,
        status=fields.get("status", "queued"),
        queued_at=fields.get("queued-at", ""),
        published_at=fields.get("published-at", "") or published_at_from_relative_age(fields.get("queued-at", ""), fields.get("age", "")),
        source=fields.get("source", ""),
        title=fields.get("title", ""),
        url=fields.get("url", ""),
        age=fields.get("age", ""),
        summary=unquote_lines(summary_lines),
        raw=raw,
    )


def parse_items(text: str) -> list[QueueItem]:
    items: list[QueueItem] = []
    for match in BLOCK_RE.finditer(text):
        item = parse_block(match.group("body"))
        if item is not None:
            items.append(item)
    return items


def ensure_header(path: Path) -> None:
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Manager non-urgent digest queue\n\n"
        "Durable queue for non-urgent PB/agent digest items. Urgent/breaking items bypass this file and may email the human directly with `[omo]`.\n\n"
        "Queued items use `(digest-item)`, not `(pending)`, so markdown pending/report watchers do not create duplicate immediate notifications.\n",
        encoding="utf-8",
    )


def append_item(path: Path, item: QueueItem) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n---\n(digest-item)\n")
        handle.write(f"id: {item.item_id}\n")
        handle.write(f"status: {item.status}\n")
        handle.write(f"queued-at: {item.queued_at}\n")
        if item.published_at:
            handle.write(f"published-at: {safe_single_line(item.published_at)}\n")
        handle.write(f"source: {safe_single_line(item.source)}\n")
        handle.write(f"title: {safe_single_line(item.title)}\n")
        handle.write(f"url: {safe_single_line(item.url)}\n")
        handle.write(f"age: {safe_single_line(item.age)}\n")
        handle.write("summary:\n")
        handle.write("\n".join(quote_lines(item.summary)))
        handle.write("\n")


def replace_statuses(path: Path, sent_ids: set[str], sent_at: str) -> None:
    text = read_text(path)
    def repl(match: re.Match[str]) -> str:
        body = match.group("body")
        item = parse_block(body)
        if item is None or item.item_id not in sent_ids or item.status != "queued":
            return match.group(0)
        body = re.sub(r"^status: queued$", "status: sent", body, count=1, flags=re.MULTILINE)
        if "sent-at:" not in body:
            body = body.rstrip("\n") + f"\nsent-at: {sent_at}\n"
        return f"---\n(digest-item)\n{body}"
    path.write_text(BLOCK_RE.sub(repl, text), encoding="utf-8")


def latest_manager_mail_time(mail_dir: Path) -> datetime | None:
    latest: datetime | None = None
    paths = list(mail_dir.glob("*.txt")) if mail_dir.exists() else []
    for path in paths:
        candidate: datetime | None = None
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:8]:
                if line.startswith("Date: "):
                    try:
                        candidate = parsedate_to_datetime(line.removeprefix("Date: ")).astimezone()
                    except (TypeError, ValueError, OSError):
                        candidate = None
                    break
            if candidate is None:
                candidate = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()
        except OSError:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def latest_outbound_time(state_dir: Path) -> datetime | None:
    path = state_dir / "human-email-sent.tsv"
    latest: datetime | None = None
    for line in read_text(path).splitlines():
        timestamp, sep, _ = line.partition("\t")
        if not sep:
            continue
        parsed = parse_iso(timestamp)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def latest_digest_sent_time(items: list[QueueItem]) -> datetime | None:
    latest: datetime | None = None
    text_times = re.findall(r"^sent-at: (.+)$", "\n".join(item.raw for item in items), flags=re.MULTILINE)
    for raw in text_times:
        parsed = parse_iso(raw)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def idle_minutes_since(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return (now - moment.astimezone()).total_seconds() / 60.0


def decide_delivery(mail_dir: Path, state_dir: Path, items: list[QueueItem], now: datetime, min_human_inbound_idle_min: int, min_manager_outbound_idle_min: int, min_delivery_gap_min: int) -> DeliveryDecision:
    reasons: list[str] = []
    if now.hour < 12:
        reasons.append("before-noon")
    if now.hour >= 23:
        reasons.append("after-evening-window")
    if not [item for item in items if item.status == "queued"]:
        reasons.append("no-queued-items")
    inbound_min = idle_minutes_since(latest_manager_mail_time(mail_dir), now)
    if inbound_min is not None and inbound_min < min_human_inbound_idle_min:
        reasons.append(f"recent-human-inbound:{inbound_min:.0f}m<{min_human_inbound_idle_min}m")
    outbound_min = idle_minutes_since(latest_outbound_time(state_dir), now)
    if outbound_min is not None and outbound_min < min_manager_outbound_idle_min:
        reasons.append(f"recent-manager-outbound:{outbound_min:.0f}m<{min_manager_outbound_idle_min}m")
    sent_min = idle_minutes_since(latest_digest_sent_time(items), now)
    if sent_min is not None and sent_min < min_delivery_gap_min:
        reasons.append(f"recent-digest-delivery:{sent_min:.0f}m<{min_delivery_gap_min}m")
    return DeliveryDecision(not reasons, reasons)


def render_digest(items: list[QueueItem]) -> str:
    lines = ["Non-urgent digest items queued for afternoon/evening idle delivery:", ""]
    for idx, item in enumerate(items, start=1):
        title = item.title or "Untitled item"
        if item.url:
            lines.append(f"{idx}. [{title}]({item.url})")
        else:
            lines.append(f"{idx}. {title}")
        if item.published_at:
            lines.append(f"   Published: {display_time(item.published_at)}")
        if item.queued_at:
            lines.append(f"   Queued: {display_time(item.queued_at)}")
        if item.source:
            lines.append(f"   Source: {item.source}")
        if item.summary:
            lines.append(f"   {item.summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def queue_path(root: Path, queue_file: Path) -> Path:
    return queue_file if queue_file.is_absolute() else root / queue_file


def lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def command_submit(args: argparse.Namespace) -> int:
    path = queue_path(args.root, args.queue_file)
    summary = args.summary or ""
    if args.summary_file:
        summary = Path(args.summary_file).read_text(encoding="utf-8")
    if not summary.strip():
        print("summary or --summary-file is required", file=sys.stderr)
        return 2
    item_id = args.id or item_hash(args.source, args.title, args.url, summary)
    if not SAFE_ID_RE.fullmatch(item_id):
        print("id must match [A-Za-z0-9._:-]{1,80}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path(path).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_header(path)
        items = parse_items(read_text(path))
        if any(item.item_id == item_id and item.status in {"queued", "sent"} for item in items):
            print(f"digest item already recorded: {item_id}")
            return 0
        queued_at = iso_now()
        published_at = args.published_at or published_at_from_relative_age(queued_at, args.age)
        append_item(path, QueueItem(item_id, "queued", queued_at, published_at, args.source, args.title, args.url, args.age, summary.strip(), ""))
    print(f"queued digest item: {item_id} file={path}")
    return 0


def command_deliver(args: argparse.Namespace) -> int:
    path = queue_path(args.root, args.queue_file)
    if args.max_items < 1:
        print("--max-items must be >= 1", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path(path).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_header(path)
        items = parse_items(read_text(path))
        queued = [item for item in items if item.status == "queued"][: args.max_items]
        if not queued:
            print("not eligible: no-queued-items")
            return 0
        body = render_digest(queued)
        if args.dry_run:
            print(body, end="")
            return 0
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="omo-digest-delivery-", suffix=".md", delete=False) as handle:
            handle.write(body)
            message_file = Path(handle.name)
        try:
            result = subprocess.run([str(args.send_helper), "--subject", args.subject, "--message-file", str(message_file)], check=False)
        finally:
            try:
                message_file.unlink()
            except OSError:
                pass
        if result.returncode != 0:
            return result.returncode
        replace_statuses(path, {item.item_id for item in queued}, iso_now())
        print(f"delivered {len(queued)} digest item(s)")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--queue-file", type=Path, default=DEFAULT_QUEUE_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--source", required=True)
    submit.add_argument("--title", required=True)
    submit.add_argument("--url", default="")
    submit.add_argument("--age", default="")
    submit.add_argument("--published-at", default="")
    submit.add_argument("--summary", default="")
    submit.add_argument("--summary-file")
    submit.add_argument("--id", default="")
    deliver = sub.add_parser("deliver-once")
    deliver.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help=argparse.SUPPRESS)
    deliver.add_argument("--mail-dir", type=Path, default=Path(DEFAULT_MAIL_DIR) if DEFAULT_MAIL_DIR else None, help=argparse.SUPPRESS)
    deliver.add_argument("--send-helper", type=Path, default=DEFAULT_SEND_HELPER)
    deliver.add_argument("--subject", default="[omo_manager] Non-urgent news digest")
    deliver.add_argument("--max-items", type=int, default=5)
    deliver.add_argument("--min-human-inbound-idle-min", type=int, default=90, help=argparse.SUPPRESS)
    deliver.add_argument("--min-manager-outbound-idle-min", type=int, default=120, help=argparse.SUPPRESS)
    deliver.add_argument("--min-delivery-gap-min", type=int, default=240, help=argparse.SUPPRESS)
    deliver.add_argument("--dry-run", action="store_true")
    deliver.add_argument("--now", default="", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    args.root = args.root.resolve()
    if args.command == "submit":
        return command_submit(args)
    if args.command == "deliver-once":
        return command_deliver(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
