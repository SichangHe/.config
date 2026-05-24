#!/usr/bin/env python3
"""Report dirty manager-owned git trees and separate active pending markdown."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def load_local_env() -> dict[str, str]:
    env = dict(os.environ)
    local_env = Path(env.get("OMO_MANAGER_LOCAL_ENV", Path.home() / ".config" / "omo_manager" / "local.env"))
    if not local_env.is_file():
        return env
    script = "set -a; source \"$1\"; env -0"
    loaded = subprocess.run(["bash", "-c", script, "bash", str(local_env)], capture_output=True, timeout=10, check=False)
    if loaded.returncode != 0:
        return env
    for item in loaded.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        raw_key, raw_value = item.split(b"=", 1)
        key = raw_key.decode(errors="ignore")
        if key and key not in os.environ:
            env[key] = raw_value.decode(errors="surrogateescape")
    return env


LOCAL_ENV = load_local_env()
DEFAULT_CONFIG = Path.home() / ".config"
DEFAULT_WORK_LOGS = Path(LOCAL_ENV.get("OMO_WORK_LOGS_ROOT", str(Path.home() / "work_logs")))
IMPLEMENTATION_SUFFIXES = {".py", ".sh", ".js", ".ts", ".json", ".jsonc", ".toml", ".yml", ".yaml"}


@dataclass(frozen=True)
class Args:
    repos: list[Path]
    fail_on_dirty_impl: bool


class ParsedArgs(argparse.Namespace):
    repo: list[Path] = []
    fail_on_dirty_impl: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, action="append", default=[])
    _ = parser.add_argument("--fail-on-dirty-impl", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    repos = parsed.repo or [DEFAULT_CONFIG, DEFAULT_WORK_LOGS]
    return Args([repo.resolve() for repo in repos], parsed.fail_on_dirty_impl)


def git_status(repo: Path) -> list[tuple[str, Path]]:
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, timeout=10, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git status failed for {repo}")
    rows: list[tuple[str, Path]] = []
    for line in out.stdout.splitlines():
        if not line:
            continue
        rows.append((line[:2], Path(line[3:])))
    return rows


def has_pending_marker(path: Path) -> bool:
    try:
        in_fence = False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if stripped == "(pending)":
                return True
        return False
    except OSError:
        return False


def classify(repo: Path, rel: Path) -> str:
    if rel.suffix == ".md" and has_pending_marker(repo / rel):
        return "active-pending-markdown"
    if rel.suffix in IMPLEMENTATION_SUFFIXES or "omo_manager" in rel.parts:
        return "manager-implementation-change"
    if rel.suffix == ".md":
        return "manager-doc-or-task-change"
    return "other-change"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    dirty_impl = False
    had_error = False
    for repo in args.repos:
        try:
            rows = git_status(repo)
        except RuntimeError as exc:
            had_error = True
            print(f"repo-error: repo={repo} error={exc}")
            continue
        if not rows:
            print(f"clean: repo={repo}")
            continue
        for status, rel in rows:
            kind = classify(repo, rel)
            dirty_impl = dirty_impl or kind == "manager-implementation-change"
            print(f"{kind}: repo={repo} status={status} file={rel}")
    return 1 if had_error or (dirty_impl and args.fail_on_dirty_impl) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
