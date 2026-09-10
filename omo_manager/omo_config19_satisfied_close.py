#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Prepare and execute the satisfied config:19 child closure."""

from __future__ import annotations

import json
from importlib import reload
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager import omo_config18_satisfied_close as close
from omo_manager.omo_task_metadata import TaskFrontmatterError

CORE = Path(close.__file__).resolve()
CORE_SHA256 = "4eb98f9b2fa32aff7fef1f1c17ade0d83a53b6a39c3ac0ba5e0dc9eb17587a77"
SCHEMA = "omo-config19-satisfied-close/v1"
REVIEW_SCHEMA = "omo-config19-satisfied-close-review/v1"
TARGET = "config:19"
TASK = "dw2_wrap_cancel.md"
UPSTREAM_AUDIT = Path("/tmp/config18-satisfied-close.q9Zzgv/audit.json")
UPSTREAM_AUDIT_SHA256 = "0894057cd73b451d277d2731aafb9300b41e79a646394d230bcc61dcdd96106d"
UPSTREAM_SCHEMA = "omo-config18-satisfied-close/v1"
PENDING_ITEM = (
    "Handle watcher problems 484cc641bff06f47f47 and 02671b57a2b872aa: implement and independently review a "
    "guarded shell-started wrapped-composer cancellation path with foreground-process binding and exact one-space "
    "blank handling for config:18/config:16; add read-only source-bound full-composer authentication for scrolled "
    "config:20 without acting on dw2:0; then recover only exact verified config:18/config:16 inputs, refresh watcher "
    "status, determine supported task closure, and report privately without Human mail."
)
PROTECTED_TARGETS = ("config:20", "dw2:0")


def validate_upstream(data: bytes) -> None:
    try:
        audit = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("config:18 committed audit is malformed.") from exc
    required = {
        "schema": UPSTREAM_SCHEMA,
        "state": "committed",
        "audit": str(UPSTREAM_AUDIT),
        "task": "config16_close.md",
        "replay_id": close.REPLAY_ID,
        "authority_sha256": close.AUTHORITY_SHA256,
        "manager_target": close.CURRENT_MANAGER,
    }
    if not isinstance(audit, dict) or any(audit.get(key) != value for key, value in required.items()):
        raise TaskFrontmatterError("config:18 audit does not prove the exact committed upstream closure.")


def configure() -> None:
    reload(close)
    if close.sha256(CORE.read_bytes()) != CORE_SHA256:
        raise TaskFrontmatterError("config:19 closure core digest changed.")
    close.SCHEMA = SCHEMA
    close.REVIEW_SCHEMA = REVIEW_SCHEMA
    close.TARGET = TARGET
    close.TASK = TASK
    close.UPSTREAM_AUDIT = UPSTREAM_AUDIT
    close.UPSTREAM_AUDIT_SHA256 = UPSTREAM_AUDIT_SHA256
    close.UPSTREAM_SCHEMA = UPSTREAM_SCHEMA
    close.UPSTREAM_LABEL = "config:18 committed audit"
    close.PENDING_ITEM = PENDING_ITEM
    close.PROTECTED_TARGETS = PROTECTED_TARGETS
    close.validate_upstream = validate_upstream


# 🧑 "You're saying they're completed, then just send a bunch of control C and kill the Tmux window, no?"
def main(argv: list[str] | None = None) -> int:
    try:
        configure()
    except (OSError, TaskFrontmatterError) as exc:
        print(f"omo_config19_satisfied_close.py: {exc}", file=__import__("sys").stderr)
        return 2
    return close.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
