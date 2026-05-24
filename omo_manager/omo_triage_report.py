#!/usr/bin/env python3
"""Summarize an agent report for manager action without forwarding blindly."""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


STATUS_NEEDS_HUMAN = {"blocked", "question", "needs-human", "needs-info"}
TRIVIAL_PATTERNS = ("where is", "what file", "which command", "pwd", "tmux", "port")


@dataclass(frozen=True)
class Args:
    report_file: Path


class ParsedArgs(argparse.Namespace):
    report_file: Path = Path()


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-file", type=Path, required=True)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.report_file)


def find_status(text: str) -> str:
    match = re.search(r"status=([^\s)]+)", text)
    return match.group(1).lower() if match else "unknown"


def decision(text: str) -> str:
    lowered = text.lower()
    status = find_status(text)
    if status in STATUS_NEEDS_HUMAN and "?" in text:
        return "ask-agent-clarify"
    if any(pattern in lowered for pattern in TRIVIAL_PATTERNS):
        return "answer-directly-if-known"
    if status in {"done", "completed", "fixed", "validated"}:
        return "record-status"
    if status in STATUS_NEEDS_HUMAN:
        return "email-human-if-manager-cannot-answer"
    return "inspect-report"


def excerpt(text: str, max_chars: int = 500) -> str:
    compact = "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())
    return compact[:max_chars]


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        text = args.report_file.read_text(encoding="utf-8")
        print(f"status={find_status(text)}")
        print(f"manager-action={decision(text)}")
        print("excerpt:")
        print(excerpt(text))
    except Exception as exc:
        print(f"omo_triage_report: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
