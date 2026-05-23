#!/usr/bin/env python3
"""Print compact OpenCode session history, newest messages first by default."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Args:
    session: str
    base_url: str
    limit: int
    reverse: bool
    input_newest_first: bool
    include_tools: bool
    text_only: bool


class ParsedArgs(argparse.Namespace):
    session: str = ""
    base_url: str = ""
    limit: int = 20
    reverse: bool = True
    include_tools: bool = False
    text_only: bool = True
    input_newest_first: bool | None = None


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--limit", type=int, default=20)
    order = parser.add_mutually_exclusive_group()
    order.add_argument("--reverse", dest="reverse", action="store_true", default=True, help="Newest first; default.")
    order.add_argument("--chronological", dest="reverse", action="store_false")
    parser.add_argument("--include-tools", action="store_true")
    parser.add_argument("--text-only", action="store_true", default=True)
    input_order = parser.add_mutually_exclusive_group()
    input_order.add_argument("--input-newest-first", dest="input_newest_first", action="store_true")
    input_order.add_argument("--input-oldest-first", dest="input_newest_first", action="store_false")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.limit < 1:
        parser.error("--limit must be positive.")
    input_newest_first = parsed.input_newest_first if parsed.input_newest_first is not None else bool(parsed.base_url)
    return Args(parsed.session, parsed.base_url.rstrip("/"), parsed.limit, parsed.reverse, input_newest_first, parsed.include_tools, parsed.text_only)


def fetch_json(args: Args) -> object:
    if args.base_url:
        url = f"{args.base_url}/session/{args.session}/message?limit={args.limit}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    out = subprocess.run(["opencode", "export", args.session], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def walk(obj: object) -> Iterable[dict[str, object]]:
    if isinstance(obj, dict):
        role = obj.get("role") or obj.get("type") or obj.get("kind")
        if role is not None and any(k in obj for k in ("text", "content", "message", "parts")):
            yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def text_from(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [text_from(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if "text" in value:
            return text_from(value["text"])
        if "content" in value:
            return text_from(value["content"])
        if "message" in value:
            return text_from(value["message"])
        if "parts" in value:
            return text_from(value["parts"])
    return ""


def is_toolish(msg: dict[str, object]) -> bool:
    raw = json.dumps(msg, sort_keys=True).lower()
    markers = ("tool", "input", "output", "bash", "grep", "read", "edit")
    role = str(msg.get("role") or msg.get("type") or msg.get("kind") or "").lower()
    return role in {"tool", "tool_call", "tool_result"} or ("tool" in role) or any(f'"{m}"' in raw for m in markers[:3])


def compact_messages(obj: object, include_tools: bool) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    seen: set[str] = set()
    for msg in walk(obj):
        if not include_tools and is_toolish(msg):
            continue
        text = text_from(msg).strip()
        if not text:
            continue
        role = str(msg.get("role") or msg.get("type") or msg.get("kind") or "message")
        ident = str(msg.get("id") or msg.get("messageID") or msg.get("message_id") or len(messages))
        created = str(msg.get("time") or msg.get("created") or msg.get("createdAt") or "")
        key = f"{role}\0{ident}\0{text[:200]}"
        if key in seen:
            continue
        seen.add(key)
        messages.append({"role": role, "id": ident, "time": created, "text": text})
    return messages


def print_messages(session: str, messages: list[dict[str, str]], limit: int, reverse: bool, input_newest_first: bool) -> None:
    newest_first = messages if input_newest_first else list(reversed(messages))
    selected = newest_first if reverse else list(reversed(newest_first))
    for msg in selected[:limit]:
        header = f"session={session} message={msg['id']} role={msg['role']}"
        if msg["time"]:
            header += f" time={msg['time']}"
        print(header)
        print(msg["text"])
        print("---")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        data = fetch_json(args)
        print_messages(args.session, compact_messages(data, args.include_tools), args.limit, args.reverse, args.input_newest_first)
    except Exception as exc:
        print(f"omo_oc_history: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
