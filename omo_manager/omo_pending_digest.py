"""Shared pending-marker digest helpers."""
from __future__ import annotations

import hashlib
from pathlib import Path


PENDING_CONTENT_CHAR_LIMIT = 6000


def truncate_content(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    body = text[:limit].rstrip()
    return f"{body}\n... [truncated {len(text) - len(body)} chars]"


def pending_tail_digest(path: Path, line: int, pending_tail: str) -> str:
    payload = truncate_content(pending_tail, PENDING_CONTENT_CHAR_LIMIT)
    return hashlib.sha256(f"{path}:{line}:{payload}".encode("utf-8")).hexdigest()[:16]
