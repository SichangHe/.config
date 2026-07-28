"""Parse versioned task-file frontmatter into shared typed metadata."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias
from uuid import UUID

import yaml

TASK_FRONTMATTER_V1 = "v1.0.0"
TASK_FRONTMATTER_V2 = "v2.0.0"
TASK_FRONTMATTER_VERSION = TASK_FRONTMATTER_V1
TASK_FRONTMATTER_STATUSES = {"running", "long_running", "blocked", "done"}
RETIRED_RUNAT = "retired"
TARGET_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b")
ID_RE = re.compile(r"^(task|pi|wake)_([0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
V1_REQUIRED_FIELDS = {"version", "status", "runat", "tool", "managerat", "is_manager", "pending_task_items"}
V1_ALLOWED_FIELDS = V1_REQUIRED_FIELDS | {"blocked_on"}
V2_REQUIRED_FIELDS = V1_REQUIRED_FIELDS | {"task_id", "resolved_task_items"}
V2_ALLOWED_FIELDS = V2_REQUIRED_FIELDS | {"blocked_on", "resume_status"}


class TaskFrontmatterError(ValueError):
    pass


@dataclass(frozen=True)
class ItemDependency:
    task_id: str
    item_id: str
    state: str


@dataclass(frozen=True)
class PendingNotice:
    id: str
    kind: str
    state: str
    recipient_task_id: str
    target_snapshot: str
    attempt_count: int
    retry_after: datetime | None
    escalated_at: datetime | None


@dataclass(frozen=True)
class PendingTaskItem:
    id: str
    text: str
    blocked_on: tuple[ItemDependency, ...]
    notices: tuple[PendingNotice, ...]


@dataclass(frozen=True)
class ResolvedTaskItem:
    id: str
    outcome: str
    evidence: str
    resolved_at: datetime
    notices: tuple[PendingNotice, ...]


@dataclass(frozen=True)
class PendingItemsBlocker:
    kind: str
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class HumanBlocker:
    kind: str
    reason: str


@dataclass(frozen=True)
class PersistentBlocker:
    kind: str
    reason: str


@dataclass(frozen=True)
class TaskBlocker:
    kind: str
    task: str
    reason: str


@dataclass(frozen=True)
class LegacyBlocker:
    kind: str
    text: str


TaskBlockerEntry: TypeAlias = PendingItemsBlocker | HumanBlocker | PersistentBlocker | TaskBlocker | LegacyBlocker


@dataclass(frozen=True)
class TaskMetadata:
    version: str
    status: str
    runat: str
    tool: str
    managerat: str
    is_manager: bool
    pending_items: tuple[PendingTaskItem, ...] = ()
    legacy_pending_items: tuple[str, ...] = ()
    blocked_on: str = ""
    task_id: str = ""
    resume_status: str = ""
    blockers: tuple[TaskBlockerEntry, ...] = ()
    resolved_task_items: tuple[ResolvedTaskItem, ...] = ()

    @property
    def pending_task_items(self) -> tuple[str, ...]:
        return self.legacy_pending_items or tuple(item.text for item in self.pending_items)


class UniqueKeyLoader(yaml.SafeLoader):
    pass


UniqueKeyLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:timestamp"] for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise TaskFrontmatterError("task frontmatter YAML mapping keys must be text.")
        if key in mapping:
            raise TaskFrontmatterError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)


def frontmatter_parts(text: str) -> tuple[list[str], list[str]] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:idx], lines[idx + 1 :]
    raise TaskFrontmatterError("task frontmatter opening marker has no closing marker.")


def frontmatter_text(text: str) -> str | None:
    parts = frontmatter_parts(text)
    return None if parts is None else "\n".join(parts[0])


def parse_task_metadata(text: str, work_log_root: Path | None = None) -> TaskMetadata | None:
    source = frontmatter_text(text)
    if source is None:
        return None
    version = first_version(source)
    if version == TASK_FRONTMATTER_V1:
        return parse_v1_metadata(source.splitlines())
    if version == TASK_FRONTMATTER_V2:
        return parse_v2_metadata(source, work_log_root)
    raise TaskFrontmatterError(f"unsupported task frontmatter version: {version or '<missing>'}")


def first_version(source: str) -> str:
    for line in source.splitlines():
        key, sep, value = line.partition(":")
        if sep and key == "version":
            return value.strip()
    return ""


def parse_v1_pending_items(lines: list[str], idx: int, value: str) -> tuple[tuple[str, ...], int]:
    if value.strip() == "[]":
        return (), idx + 1
    if value.strip():
        raise TaskFrontmatterError("`pending_task_items` must be `[]` or a YAML list.")
    items: list[str] = []
    idx += 1
    while idx < len(lines) and lines[idx].startswith("  - "):
        item = lines[idx][4:].strip()
        if not item:
            raise TaskFrontmatterError("`pending_task_items` entries must not be empty.")
        items.append(item)
        idx += 1
    return tuple(items), idx


def parse_v1_metadata(lines: list[str]) -> TaskMetadata:
    values: dict[str, str | bool | tuple[str, ...]] = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not line:
            raise TaskFrontmatterError("task frontmatter must not contain blank lines.")
        if line.startswith("  - "):
            raise TaskFrontmatterError("YAML list item is only valid under `pending_task_items`.")
        key, sep, value = line.partition(":")
        if not sep or not key or key.strip() != key:
            raise TaskFrontmatterError(f"invalid task frontmatter line: {line}")
        if key not in V1_ALLOWED_FIELDS:
            raise TaskFrontmatterError(f"unknown task frontmatter field: {key}")
        if key in values:
            raise TaskFrontmatterError(f"duplicate task frontmatter field: {key}")
        if key == "pending_task_items":
            values[key], idx = parse_v1_pending_items(lines, idx, value)
            continue
        stripped = value.strip()
        if not stripped:
            raise TaskFrontmatterError(f"`{key}` must not be empty.")
        if key == "is_manager":
            if stripped not in {"true", "false"}:
                raise TaskFrontmatterError("`is_manager` must be `true` or `false`.")
            values[key] = stripped == "true"
        else:
            values[key] = stripped
        idx += 1
    validate_required(values, V1_REQUIRED_FIELDS)
    common = parse_common(values, V1_ALLOWED_FIELDS)
    blocked_on = values.get("blocked_on", "")
    if not isinstance(blocked_on, str):
        raise TaskFrontmatterError("`blocked_on` must be text.")
    pending_items = values["pending_task_items"]
    if not isinstance(pending_items, tuple):
        raise TaskFrontmatterError("`pending_task_items` must be a list.")
    return TaskMetadata(*common, legacy_pending_items=pending_items, blocked_on=blocked_on)


def load_v2_mapping(source: str) -> dict[str, object]:
    try:
        loaded = yaml.load(source, Loader=UniqueKeyLoader)
    except TaskFrontmatterError:
        raise
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise TaskFrontmatterError(f"invalid task frontmatter YAML: {exc}") from exc
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise TaskFrontmatterError("task frontmatter must be a mapping with text keys.")
    return loaded


def parse_v2_metadata(source: str, work_log_root: Path | None = None) -> TaskMetadata:
    values = load_v2_mapping(source)
    validate_required(values, V2_REQUIRED_FIELDS)
    common = parse_common(values, V2_ALLOWED_FIELDS)
    task_id = require_id(values["task_id"], "task", "task_id")
    pending_items = tuple(parse_pending_item(value) for value in require_list(values["pending_task_items"], "pending_task_items"))
    resolved_items = tuple(parse_resolved_item(value) for value in require_list(values["resolved_task_items"], "resolved_task_items"))
    all_item_ids = [item.id for item in pending_items] + [item.id for item in resolved_items]
    if len(set(all_item_ids)) != len(all_item_ids):
        raise TaskFrontmatterError("pending and resolved item ids must be unique within a task.")
    blockers = tuple(parse_blocker(value, work_log_root) for value in require_list(values.get("blocked_on", []), "blocked_on"))
    if common[1] in {"blocked", "long_running"} and not blockers:
        raise TaskFrontmatterError("`blocked_on` must contain at least one blocker for a blocked or long_running v2 task.")
    if common[1] == "long_running" and any(not isinstance(blocker, PersistentBlocker) for blocker in blockers):
        raise TaskFrontmatterError("a `long_running` v2 task may only have persistent blockers.")
    notices = [notice for item in (*pending_items, *resolved_items) for notice in item.notices]
    if len({notice.id for notice in notices}) != len(notices):
        raise TaskFrontmatterError("notice ids must be unique within a task.")
    if any(notice.recipient_task_id != task_id for notice in notices):
        raise TaskFrontmatterError("notice `recipient_task_id` must equal the owning task id.")
    status = common[1]
    resume_status = values.get("resume_status", "")
    if status == "blocked":
        if resume_status not in {"running", "long_running"}:
            raise TaskFrontmatterError("`resume_status` must be `running` or `long_running` for a blocked v2 task.")
    elif "resume_status" in values:
        raise TaskFrontmatterError("`resume_status` must only exist when `status` is `blocked`.")
    blocker_summary = "; ".join(blocker_text(blocker) for blocker in blockers)
    return TaskMetadata(*common, pending_items=pending_items, blocked_on=blocker_summary, task_id=task_id, resume_status=str(resume_status), blockers=blockers, resolved_task_items=resolved_items)


def validate_required(values: Mapping[str, object], required: set[str]) -> None:
    missing = required - values.keys()
    if missing:
        raise TaskFrontmatterError(f"missing task frontmatter field: {sorted(missing)[0]}")


def parse_common(values: Mapping[str, object], allowed: set[str]) -> tuple[str, str, str, str, str, bool]:
    extra = values.keys() - allowed
    if extra:
        raise TaskFrontmatterError(f"unknown task frontmatter field: {sorted(extra)[0]}")
    version = require_text(values["version"], "version")
    status = require_text(values["status"], "status")
    if status not in TASK_FRONTMATTER_STATUSES:
        raise TaskFrontmatterError("`status` must be `running`, `long_running`, `blocked`, or `done`.")
    has_blocked_on = "blocked_on" in values
    if status in {"blocked", "long_running"} and not has_blocked_on:
        raise TaskFrontmatterError("`blocked_on` is required when `status` is `blocked` or `long_running`.")
    if status not in {"blocked", "long_running"} and has_blocked_on:
        raise TaskFrontmatterError("`blocked_on` must only exist when `status` is `blocked` or `long_running`.")
    runat = require_text(values["runat"], "runat")
    managerat = require_text(values["managerat"], "managerat")
    if TARGET_RE.fullmatch(runat) is None and runat != RETIRED_RUNAT:
        raise TaskFrontmatterError("`runat` must be a tmux target or `retired`.")
    if runat == RETIRED_RUNAT and status != "blocked":
        raise TaskFrontmatterError("`runat: retired` is only valid when `status` is `blocked`.")
    if TARGET_RE.fullmatch(managerat) is None:
        raise TaskFrontmatterError("`managerat` must be a tmux target.")
    if canonical_target(runat) == canonical_target(managerat):
        raise TaskFrontmatterError("`managerat` must be different from `runat`.")
    tool = require_text(values["tool"], "tool")
    is_manager = values["is_manager"]
    if not isinstance(is_manager, bool):
        raise TaskFrontmatterError("`is_manager` must be a boolean.")
    return version, status, runat, tool, managerat, is_manager


def canonical_target(target: str) -> str:
    return target.removesuffix(".0")


def require_mapping(value: object, field: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TaskFrontmatterError(f"`{field}` entries must be mappings.")
    extra = value.keys() - keys
    missing = keys - value.keys()
    if missing or extra:
        problem = f"missing `{sorted(missing)[0]}`" if missing else f"unknown `{sorted(extra)[0]}`"
        raise TaskFrontmatterError(f"`{field}` entry has {problem}.")
    return value


def require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TaskFrontmatterError(f"`{field}` must be a list.")
    return value


def require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TaskFrontmatterError(f"`{field}` must be nonempty text.")
    return value


def require_id(value: object, prefix: str, field: str) -> str:
    text = require_text(value, field)
    match = ID_RE.fullmatch(text)
    if match is None or match.group(1) != prefix or UUID(match.group(2)).version != 7:
        raise TaskFrontmatterError(f"`{field}` must be a canonical `{prefix}_` UUIDv7 id.")
    return text


def require_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or RFC3339_RE.fullmatch(value) is None:
        raise TaskFrontmatterError(f"`{field}` must be an RFC 3339 time with an explicit offset.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskFrontmatterError(f"`{field}` must be an RFC 3339 time with an explicit offset.") from exc


def parse_dependency(value: object) -> ItemDependency:
    row = require_mapping(value, "blocked_on", {"task_id", "item_id", "state"})
    state = require_text(row["state"], "state")
    if state not in {"waiting", "cancelled"}:
        raise TaskFrontmatterError("dependency `state` must be `waiting` or `cancelled`.")
    return ItemDependency(require_id(row["task_id"], "task", "task_id"), require_id(row["item_id"], "pi", "item_id"), state)


def parse_notice(value: object) -> PendingNotice:
    keys = {"id", "kind", "state", "recipient_task_id", "target_snapshot", "attempt_count", "retry_after", "escalated_at"}
    row = require_mapping(value, "notices", keys)
    kind = require_text(row["kind"], "kind")
    state = require_text(row["state"], "state")
    if kind not in {"ready", "dependency_cancelled", "cycle_repair"}:
        raise TaskFrontmatterError("notice `kind` must be `ready`, `dependency_cancelled`, or `cycle_repair`.")
    if state not in {"deferred", "pending", "acked", "superseded"}:
        raise TaskFrontmatterError("notice `state` is invalid.")
    attempts = row["attempt_count"]
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise TaskFrontmatterError("notice `attempt_count` must be a nonnegative integer.")
    retry_after = None if row["retry_after"] is None else require_time(row["retry_after"], "retry_after")
    escalated_at = None if row["escalated_at"] is None else require_time(row["escalated_at"], "escalated_at")
    return PendingNotice(
        require_id(row["id"], "wake", "notice id"),
        kind,
        state,
        require_id(row["recipient_task_id"], "task", "recipient_task_id"),
        require_text(row["target_snapshot"], "target_snapshot"),
        attempts,
        retry_after,
        escalated_at,
    )


def parse_pending_item(value: object) -> PendingTaskItem:
    row = require_mapping(value, "pending_task_items", {"id", "text", "blocked_on", "notices"})
    return PendingTaskItem(
        require_id(row["id"], "pi", "pending item id"),
        require_text(row["text"], "pending item text"),
        tuple(parse_dependency(item) for item in require_list(row["blocked_on"], "blocked_on")),
        tuple(parse_notice(item) for item in require_list(row["notices"], "notices")),
    )


def parse_resolved_item(value: object) -> ResolvedTaskItem:
    row = require_mapping(value, "resolved_task_items", {"id", "outcome", "evidence", "resolved_at", "notices"})
    outcome = require_text(row["outcome"], "outcome")
    if outcome not in {"completed", "cancelled"}:
        raise TaskFrontmatterError("resolved item `outcome` must be `completed` or `cancelled`.")
    return ResolvedTaskItem(
        require_id(row["id"], "pi", "resolved item id"),
        outcome,
        require_text(row["evidence"], "evidence"),
        require_time(row["resolved_at"], "resolved_at"),
        tuple(parse_notice(item) for item in require_list(row["notices"], "notices")),
    )


def canonical_task_reference(value: object, work_log_root: Path | None) -> str:
    text = require_text(value, "task")
    path = Path(text)
    if path.is_absolute() or "\\" in text or path.as_posix() != text or path.suffix != ".md" or any(part in {"", ".", ".."} for part in path.parts):
        raise TaskFrontmatterError("task blocker `task` must be a canonical relative Markdown path inside the work-log root.")
    if work_log_root is not None:
        root = work_log_root.resolve()
        resolved = (root / path).resolve(strict=False)
        if resolved == root or not resolved.is_relative_to(root):
            raise TaskFrontmatterError("task blocker `task` must resolve inside the work-log root.")
    return text


def parse_blocker(value: object, work_log_root: Path | None = None) -> TaskBlockerEntry:
    if not isinstance(value, dict) or set(value) == set() or not isinstance(value.get("kind"), str):
        raise TaskFrontmatterError("`blocked_on` entries must be mappings with a `kind`.")
    kind = value["kind"]
    if kind == "pending_items":
        row = require_mapping(value, "blocked_on", {"kind", "item_ids"})
        item_ids = tuple(require_id(item, "pi", "item_id") for item in require_list(row["item_ids"], "item_ids"))
        if not item_ids:
            raise TaskFrontmatterError("a `pending_items` blocker requires at least one item id.")
        return PendingItemsBlocker(kind, item_ids)
    if kind == "human":
        row = require_mapping(value, "blocked_on", {"kind", "reason"})
        return HumanBlocker(kind, require_text(row["reason"], "reason"))
    if kind == "persistent":
        row = require_mapping(value, "blocked_on", {"kind", "reason"})
        return PersistentBlocker(kind, require_text(row["reason"], "reason"))
    if kind == "task":
        row = require_mapping(value, "blocked_on", {"kind", "task", "reason"})
        return TaskBlocker(kind, canonical_task_reference(row["task"], work_log_root), require_text(row["reason"], "reason"))
    if kind == "legacy":
        row = require_mapping(value, "blocked_on", {"kind", "text"})
        return LegacyBlocker(kind, require_text(row["text"], "text"))
    raise TaskFrontmatterError(f"unknown blocker kind: {kind}")


def blocker_text(blocker: TaskBlockerEntry) -> str:
    if isinstance(blocker, PendingItemsBlocker):
        return "pending_items"
    if isinstance(blocker, (HumanBlocker, PersistentBlocker)):
        return blocker.reason
    if isinstance(blocker, TaskBlocker):
        return blocker.reason
    return blocker.text
