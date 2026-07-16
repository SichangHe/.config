"""Shared pending-marker digest helpers."""
from __future__ import annotations

import hashlib
from pathlib import Path


PENDING_CONTENT_CHAR_LIMIT = 2000
DIRECT_MESSAGE_CONTENT_CHAR_LIMIT = 12000


def truncate_content(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted_n = len(text) - limit
    while True:
        marker = f"…{omitted_n}chars…"
        keep = max(0, limit - len(marker))
        next_omitted_n = len(text) - keep
        if next_omitted_n == omitted_n:
            break
        omitted_n = next_omitted_n
    head_n = keep // 2
    tail_n = keep - head_n
    return f"{text[:head_n].rstrip()}{marker}{text[-tail_n:].lstrip()}"


def pending_tail_digest(path: Path, line: int, pending_tail: str) -> str:
    return hashlib.sha256(f"{path}:{line}:{pending_tail}".encode("utf-8")).hexdigest()[:16]
