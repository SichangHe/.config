#!/usr/bin/env python3
"""Load the split agent-to-human Gmail configuration."""
from __future__ import annotations

import os
import hashlib
import re
import stat
import tempfile
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path

LOCAL_ENV_PATH = Path(os.environ.get("OMO_MANAGER_LOCAL_ENV", Path.home() / ".config/omo_manager/local.env"))
AGENT_ADDRESS_KEY = "OMO_AGENT_GMAIL_ADDRESS"
AGENT_PASSWORD_KEY = "OMO_AGENT_GMAIL_APP_PASSWORD"
HUMAN_ADDRESS_KEY = "OMO_HUMAN_EMAIL_ADDRESS"
HUMAN_CONFIG_PATH_KEY = "OMO_HUMAN_EMAIL_CONFIG_PATH"
GMAIL_IMAP_HOST = "imap.gmail.com"
# 🧑 "Replace them. ... make sure that in the future replies get sent to the guest also"
GUEST_HEES_ADDRESS = "46496337@qq.com"
GUEST_HEES_SESSION = "guest_hees"
GUEST_HEES_MAIL_DIRNAME = "guest_hees_manager_mail"
GUEST_HEES_ACTIVE_STATUSES = frozenset({"running", "long_running"})
TMUX_TARGET_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\Z")


@dataclass(frozen=True)
class AgentMailSettings:
    agent_address: str
    app_password: str
    human_address: str


@dataclass(frozen=True)
class GuestHeesOwner:
    task_file: Path
    target: str


def guest_hees_target(target: str | None) -> bool:
    """Return whether one canonical tmux target is inside the guest session."""
    if target is None or TMUX_TARGET_RE.fullmatch(target) is None:
        return False
    return target.partition(":")[0] == GUEST_HEES_SESSION


def active_guest_hees_owner(root: Path) -> GuestHeesOwner:
    """Resolve exactly one active, sendable guest manager from current TODO custody."""
    try:
        from .omo_agent_status import parse_task_lines, read_task_metadata, resolve_task_path, same_tmux_target
    except ImportError:
        from omo_agent_status import parse_task_lines, read_task_metadata, resolve_task_path, same_tmux_target

    owners: list[GuestHeesOwner] = []
    for task in parse_task_lines(root / "TODO.md"):
        if task.section != "todo:current" or not guest_hees_target(task.target):
            continue
        path = resolve_task_path(root, task.task_file)
        metadata = read_task_metadata(path, root)
        if (
            path is None
            or metadata is None
            or not metadata.is_manager
            or metadata.status not in GUEST_HEES_ACTIVE_STATUSES
            or not guest_hees_target(metadata.runat)
            or not same_tmux_target(task.target, metadata.runat)
        ):
            continue
        owners.append(GuestHeesOwner(path.resolve(), metadata.runat))
    if len(owners) != 1:
        raise RuntimeError(f"guest-hees owner resolution requires exactly one active manager; found {len(owners)}")
    owner = owners[0]
    try:
        from .omo_tmux_send import require_sendable_codex_target
    except ImportError:
        from omo_tmux_send import require_sendable_codex_target
    try:
        require_sendable_codex_target(owner.target)
    except Exception as exc:
        raise RuntimeError(f"guest-hees owner is not sendable: {owner.target}: {exc}") from exc
    return owner


def guest_hees_owner_is_current(root: Path, owner: GuestHeesOwner) -> bool:
    """Revalidate a binding across asynchronous delivery."""
    try:
        return active_guest_hees_owner(root) == owner
    except RuntimeError:
        return False


def guest_hees_intake_receipt_path(state_dir: Path, source: str) -> Path:
    """Return the private receipt path for one durable guest mail artifact."""
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return state_dir / "guest-hees-intake-delivered" / f"{digest}.receipt"


def guest_hees_intake_delivery_owner(state_dir: Path, source: str) -> GuestHeesOwner | None:
    """Validate the full owner identity in one durable intake receipt."""
    path = guest_hees_intake_receipt_path(state_dir, source)
    try:
        metadata = path.lstat()
        fields = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values = dict(field.split("=", 1) for field in fields if "=" in field)
    task_file = Path(values.get("task_file", ""))
    target = values.get("target", "")
    valid = (
        path.is_file()
        and metadata.st_uid == os.getuid()
        and not metadata.st_mode & 0o077
        and len(fields) == 3
        and len(values) == 3
        and values.get("source") == source
        and task_file.is_absolute()
        and task_file.resolve(strict=False) == task_file
        and guest_hees_target(target)
    )
    return GuestHeesOwner(task_file, target) if valid else None


def guest_hees_intake_is_delivered(state_dir: Path, source: str, owner: GuestHeesOwner | None = None) -> bool:
    """Return whether the current owner accepted this exact artifact."""
    delivered_owner = guest_hees_intake_delivery_owner(state_dir, source)
    return delivered_owner is not None and (owner is None or delivered_owner == owner)


@dataclass(frozen=True)
class GuestHeesReplyObligation:
    source: str
    inbound_message_id: str
    status: str
    outbound_message_id: str = ""
    outbound_to: str = ""
    subject_sha256: str = ""
    body_sha256: str = ""


def guest_hees_reply_obligation_path(state_dir: Path, source: str) -> Path:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return state_dir / "guest-hees-reply-obligations" / f"{digest}.state"


def _private_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077


def read_guest_hees_reply_obligation(state_dir: Path, source: str) -> GuestHeesReplyObligation | None:
    path = guest_hees_reply_obligation_path(state_dir, source)
    if not _private_file(path):
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        values = dict(line.split("=", 1) for line in lines)
    except (OSError, ValueError):
        return None
    expected = {"version", "source", "inbound_message_id", "status"}
    if values.get("status") == "fulfilled":
        expected.update({"outbound_message_id", "outbound_to", "subject_sha256", "body_sha256", "sent_verified"})
    if len(values) != len(lines) or set(values) != expected or values.get("version") != "v1" or values.get("source") != source:
        return None
    inbound = values.get("inbound_message_id", "")
    outbound = values.get("outbound_message_id", "")
    status_value = values.get("status", "")
    if (
        re.fullmatch(r"<[^<>\s]+>", inbound) is None
        or (outbound and re.fullmatch(r"<[^<>\s]+>", outbound) is None)
        or status_value == "fulfilled"
        and (
            values.get("outbound_to") != GUEST_HEES_ADDRESS
            or re.fullmatch(r"[0-9a-f]{64}", values.get("subject_sha256", "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", values.get("body_sha256", "")) is None
            or values.get("sent_verified") != "true"
        )
    ):
        return None
    if status_value not in {"open", "fulfilled"} or (status_value == "fulfilled") != bool(outbound):
        return None
    return GuestHeesReplyObligation(
        source,
        inbound,
        status_value,
        outbound,
        values.get("outbound_to", ""),
        values.get("subject_sha256", ""),
        values.get("body_sha256", ""),
    )


def _publish_private(path: Path, payload: str, *, replace: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        temporary.chmod(0o600)
        _ = handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_guest_hees_reply_obligation(state_dir: Path, source: str, inbound_message_id: str) -> bool:
    """Create one immutable open reply obligation for authenticated intake."""
    if re.fullmatch(r"<[^<>\s]+>", inbound_message_id) is None:
        return False
    expected = GuestHeesReplyObligation(source, inbound_message_id, "open")
    current = read_guest_hees_reply_obligation(state_dir, source)
    if current is not None:
        return current == expected or (current.inbound_message_id == inbound_message_id and current.status == "fulfilled")
    path = guest_hees_reply_obligation_path(state_dir, source)
    payload = f"version=v1\nsource={source}\ninbound_message_id={inbound_message_id}\nstatus=open\n"
    try:
        _publish_private(path, payload)
    except FileExistsError:
        pass
    except OSError:
        return False
    return read_guest_hees_reply_obligation(state_dir, source) == expected


def guest_hees_reply_is_fulfilled(state_dir: Path, source: str) -> bool:
    obligation = read_guest_hees_reply_obligation(state_dir, source)
    return obligation is not None and obligation.status == "fulfilled"


def open_guest_hees_reply_source(state_dir: Path, inbound_message_id: str) -> str:
    """Resolve exactly one open request for a direct-thread reply."""
    directory = state_dir / "guest-hees-reply-obligations"
    matches: list[GuestHeesReplyObligation] = []
    for path in directory.glob("*.state") if directory.is_dir() else ():
        try:
            source = next(line.removeprefix("source=") for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("source="))
        except (OSError, StopIteration):
            continue
        obligation = read_guest_hees_reply_obligation(state_dir, source)
        if obligation is not None and obligation.status == "open" and obligation.inbound_message_id == inbound_message_id:
            matches.append(obligation)
    if len(matches) != 1:
        raise OSError(f"verified guest reply must match exactly one open intake obligation; found {len(matches)}")
    return matches[0].source


def fulfill_guest_hees_reply_obligation(
    state_dir: Path,
    inbound_message_id: str,
    outbound_message_id: str,
    subject_sha256: str,
    body_sha256: str,
) -> str:
    """Fulfill exactly one open request with its verified direct-thread reply."""
    source = open_guest_hees_reply_source(state_dir, inbound_message_id)
    obligation = read_guest_hees_reply_obligation(state_dir, source)
    if (
        obligation is None
        or re.fullmatch(r"<[^<>\s]+>", outbound_message_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", subject_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", body_sha256) is None
    ):
        raise OSError("verified guest reply evidence is invalid")
    path = guest_hees_reply_obligation_path(state_dir, source)
    payload = (
        f"version=v1\nsource={obligation.source}\ninbound_message_id={obligation.inbound_message_id}\n"
        f"status=fulfilled\noutbound_message_id={outbound_message_id}\noutbound_to={GUEST_HEES_ADDRESS}\n"
        f"subject_sha256={subject_sha256}\nbody_sha256={body_sha256}\nsent_verified=true\n"
    )
    _publish_private(path, payload, replace=True)
    fulfilled = read_guest_hees_reply_obligation(state_dir, obligation.source)
    if fulfilled is None or fulfilled.status != "fulfilled" or fulfilled.outbound_message_id != outbound_message_id:
        raise OSError("guest reply obligation fulfillment was not durable")
    return obligation.source


def guest_hees_mail(settings: AgentMailSettings) -> AgentMailSettings:
    """Return the pinned guest intake/reply profile over the agent mailbox."""
    return AgentMailSettings(settings.agent_address, settings.app_password, GUEST_HEES_ADDRESS)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple shell-style `KEY=value` and `export KEY=value` rows."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, value = line.split("=", 1)
        clean_value = value.strip()
        if len(clean_value) >= 2 and clean_value[0] == clean_value[-1] and clean_value[0] in {'"', "'"}:
            clean_value = clean_value[1:-1]
        values[key.strip()] = os.path.expanduser(os.path.expandvars(clean_value))
    return values


def local_email_values(path: Path = LOCAL_ENV_PATH) -> dict[str, str]:
    """Return local email values with process environment taking precedence."""
    values = parse_env_file(path)
    for key in (AGENT_ADDRESS_KEY, AGENT_PASSWORD_KEY, HUMAN_ADDRESS_KEY, HUMAN_CONFIG_PATH_KEY):
        if os.environ.get(key, "").strip():
            values[key] = os.environ[key]
    return values


def configured_agent_mail(path: Path = LOCAL_ENV_PATH) -> AgentMailSettings | None:
    """Return split-mail settings, reject partial or same-address configuration."""
    values = local_email_values(path)
    configured = {
        AGENT_ADDRESS_KEY: values.get(AGENT_ADDRESS_KEY, "").strip(),
        AGENT_PASSWORD_KEY: values.get(AGENT_PASSWORD_KEY, "").strip(),
        HUMAN_ADDRESS_KEY: values.get(HUMAN_ADDRESS_KEY, "").strip(),
    }
    if not any(configured.values()):
        return None
    missing = sorted(key for key, value in configured.items() if not value)
    if missing:
        raise ValueError(f"incomplete split email configuration; missing {missing} in {path}")
    agent_address = configured[AGENT_ADDRESS_KEY]
    human_address = configured[HUMAN_ADDRESS_KEY]
    for label, address in (("agent", agent_address), ("human", human_address)):
        parsed = getaddresses([address])
        if len(parsed) != 1 or parsed[0][1].casefold() != address.casefold() or "@" not in address or any(ch.isspace() for ch in address):
            raise ValueError(f"invalid {label} email address")
    if agent_address.casefold() == human_address.casefold():
        raise ValueError("agent and human email addresses must differ")
    return AgentMailSettings(agent_address, configured[AGENT_PASSWORD_KEY], human_address)


def human_config_path(path: Path = LOCAL_ENV_PATH) -> Path:
    """Return the Himalaya config retained for human-mail cleanup."""
    values = local_email_values(path)
    raw = values.get(HUMAN_CONFIG_PATH_KEY) or os.environ.get("OMO_EMAIL_CONFIG_PATH") or str(Path.home() / ".config/himalaya/config.toml")
    return Path(raw).expanduser()
