#!/usr/bin/env python3
"""Durable manager claims for watcher-detected problems."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

PROBLEM_ID_RE = re.compile(r"^[0-9a-f]{16}$")
DEFAULT_CLAIM_LEASE_S = 600.0


def canonical_target(target: str) -> str:
    return target[:-2] if target.endswith(".0") else target


@dataclass(frozen=True)
class ProblemClaim:
    problem_id: str
    manager_target: str
    action: str
    claimed_at_s: float
    expires_at_s: float


@dataclass(frozen=True)
class ProblemIssue:
    problem_id: str
    manager_target: str
    issued_at_s: float
    problem_lines: tuple[str, ...]


@dataclass
class ProblemState:
    issues: dict[str, ProblemIssue]
    claims: dict[str, ProblemClaim]


def default_claim_path() -> Path:
    state_dir = Path(os.environ.get("OMO_MANAGER_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))
    return state_dir / "amh-problem-claims.json"


def issue_path(claim_path: Path) -> Path:
    return claim_path


def legacy_issue_path(claim_path: Path) -> Path:
    return claim_path.with_name("amh-problem-issues.json")


def read_issues(path: Path) -> dict[str, ProblemIssue]:
    return read_state(path).issues


def parse_issues(raw: object) -> dict[str, ProblemIssue]:
    if not isinstance(raw, dict):
        raise ValueError("invalid AMH problem issues")
    issues: dict[str, ProblemIssue] = {}
    for problem_id, value in raw.items():
        if not isinstance(problem_id, str) or PROBLEM_ID_RE.fullmatch(problem_id) is None or not isinstance(value, dict):
            raise ValueError(f"invalid AMH problem issue: {problem_id!r}")
        try:
            lines = value["problem_lines"]
            manager_target = value["manager_target"]
            issued_at_s = value["issued_at_s"]
            if value.get("problem_id") != problem_id or not isinstance(manager_target, str) or not manager_target:
                raise ValueError
            if isinstance(issued_at_s, bool) or not isinstance(issued_at_s, (int, float)) or not math.isfinite(issued_at_s):
                raise ValueError
            if not isinstance(lines, list) or not lines or not all(isinstance(line, str) and line for line in lines):
                raise ValueError
            issue = ProblemIssue(problem_id, manager_target, float(issued_at_s), tuple(lines))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid AMH problem issue: {problem_id}") from exc
        issues[problem_id] = issue
    return issues


def parse_claims(raw: object) -> dict[str, ProblemClaim]:
    if not isinstance(raw, dict):
        raise ValueError("invalid AMH problem claims")
    claims: dict[str, ProblemClaim] = {}
    for problem_id, value in raw.items():
        if not isinstance(problem_id, str) or PROBLEM_ID_RE.fullmatch(problem_id) is None or not isinstance(value, dict):
            raise ValueError(f"invalid AMH problem claim: {problem_id!r}")
        try:
            manager_target = value["manager_target"]
            action = value["action"]
            claimed_at_s = value["claimed_at_s"]
            expires_at_s = value["expires_at_s"]
            if value.get("problem_id") != problem_id or not isinstance(manager_target, str) or not manager_target or not isinstance(action, str) or not action:
                raise ValueError
            if action != " ".join(action.split()) or len(action) > 240 or ";" in action:
                raise ValueError
            if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in (claimed_at_s, expires_at_s)):
                raise ValueError
            if not math.isclose(expires_at_s - claimed_at_s, DEFAULT_CLAIM_LEASE_S, rel_tol=0.0, abs_tol=0.001):
                raise ValueError
            claim = ProblemClaim(problem_id, manager_target, action, float(claimed_at_s), float(expires_at_s))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid AMH problem claim: {problem_id}") from exc
        claims[problem_id] = claim
    return claims


def read_state(path: Path) -> ProblemState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:
            legacy_raw = json.loads(legacy_issue_path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ProblemState({}, {})
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read legacy AMH problem state: {legacy_issue_path(path)}") from exc
        return ProblemState(parse_issues(legacy_raw), {})
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read AMH problem state: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"invalid AMH problem state: {path}")
    if "version" not in raw:
        try:
            legacy_raw = json.loads(legacy_issue_path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            legacy_raw = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read legacy AMH problem state: {legacy_issue_path(path)}") from exc
        if not isinstance(legacy_raw, dict):
            raise ValueError(f"invalid legacy AMH problem state: {legacy_issue_path(path)}")
        return ProblemState(parse_issues(legacy_raw), parse_claims(raw))
    if raw.get("version") != 1:
        raise ValueError(f"unsupported AMH problem-state version: {raw.get('version')!r}")
    return ProblemState(parse_issues(raw.get("issues")), parse_claims(raw.get("claims")))


def read_claims(path: Path) -> dict[str, ProblemClaim]:
    return read_state(path).claims


def write_state(path: Path, state: ProblemState) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": 1,
                    "issues": {key: asdict(value) for key, value in sorted(state.issues.items())},
                    "claims": {key: asdict(value) for key, value in sorted(state.claims.items())},
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        legacy_issue_path(path).unlink(missing_ok=True)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked_problem_state(path: Path) -> Iterator[ProblemState]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot inspect AMH problem state: {path}") from exc
        needs_migration = legacy_issue_path(path).exists() or isinstance(raw, dict) and bool(raw) and "version" not in raw
        state = read_state(path)
        before = ProblemState(state.issues.copy(), state.claims.copy())
        yield state
        if state != before or needs_migration:
            write_state(path, state)


@contextmanager
def locked_claims(path: Path) -> Iterator[dict[str, ProblemClaim]]:
    with locked_problem_state(path) as state:
        yield state.claims


def sync_problem_issues(claim_path: Path, active_problems: dict[str, tuple[str, tuple[str, ...]]], now_s: float) -> None:
    with locked_problem_state(claim_path) as state:
        previous = state.issues
        current_lines_by_owner: dict[str, set[str]] = {}
        for manager_target, problem_lines in active_problems.values():
            current_lines_by_owner.setdefault(canonical_target(manager_target), set()).update(problem_lines)
        issues = {}
        for problem_id, (manager_target, problem_lines) in active_problems.items():
            issued_at_s = previous[problem_id].issued_at_s if problem_id in previous else now_s
            issues[problem_id] = ProblemIssue(problem_id, manager_target, issued_at_s, problem_lines)
        for problem_id, issue in previous.items():
            current_lines = current_lines_by_owner.get(canonical_target(issue.manager_target), set())
            if problem_id not in issues and set(issue.problem_lines) <= current_lines:
                issues[problem_id] = issue
        state.issues = issues
        for problem_id in tuple(state.claims):
            if problem_id not in issues:
                del state.claims[problem_id]


def issue_problem(claim_path: Path, problem_id: str, manager_target: str, problem_lines: tuple[str, ...], now_s: float) -> None:
    with locked_problem_state(claim_path) as state:
        previous = state.issues.get(problem_id)
        state.issues[problem_id] = ProblemIssue(problem_id, manager_target, previous.issued_at_s if previous is not None else now_s, problem_lines)


def claim_problem(path: Path, problem_id: str, manager_target: str, action: str, now_s: float | None = None) -> ProblemClaim:
    if PROBLEM_ID_RE.fullmatch(problem_id) is None:
        raise ValueError("problem ID must be 16 lowercase hexadecimal characters")
    clean_action = " ".join(action.split())
    if not clean_action:
        raise ValueError("claim requires one concrete next action")
    if "\n" in action or ";" in action:
        raise ValueError("claim action must be one line without command separators")
    if len(clean_action) > 240:
        raise ValueError("claim action must be at most 240 characters")
    if not manager_target:
        raise ValueError("claim requires the current manager tmux target")
    claimed_at_s = time.time() if now_s is None else now_s
    claim = ProblemClaim(problem_id, manager_target, clean_action, claimed_at_s, claimed_at_s + DEFAULT_CLAIM_LEASE_S)
    with locked_problem_state(path) as state:
        issue = state.issues.get(problem_id)
        if issue is None:
            raise ValueError("problem ID is not currently issued by the watcher")
        if canonical_target(issue.manager_target) != canonical_target(manager_target):
            raise ValueError(f"problem is issued to {issue.manager_target}, not the current manager pane")
        existing = state.claims.get(problem_id)
        if existing is not None and canonical_target(existing.manager_target) == canonical_target(manager_target) and existing.expires_at_s > claimed_at_s:
            raise ValueError("this unchanged problem was already claimed; wait for the watcher to resolve it or report it again")
        state.claims[problem_id] = claim
    return claim


def remove_claim(path: Path, problem_id: str) -> None:
    with locked_claims(path) as claims:
        claims.pop(problem_id, None)


def prune_resolved_claims(path: Path, active_problem_ids: set[str]) -> None:
    with locked_claims(path) as claims:
        for problem_id in tuple(claims):
            if problem_id not in active_problem_ids:
                del claims[problem_id]
