#!/usr/bin/env python3
"""Load the split agent-to-human Gmail configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path

LOCAL_ENV_PATH = Path(os.environ.get("OMO_MANAGER_LOCAL_ENV", Path.home() / ".config/omo_manager/local.env"))
AGENT_ADDRESS_KEY = "OMO_AGENT_GMAIL_ADDRESS"
AGENT_PASSWORD_KEY = "OMO_AGENT_GMAIL_APP_PASSWORD"
HUMAN_ADDRESS_KEY = "OMO_HUMAN_EMAIL_ADDRESS"
HUMAN_CONFIG_PATH_KEY = "OMO_HUMAN_EMAIL_CONFIG_PATH"
GMAIL_IMAP_HOST = "imap.gmail.com"


@dataclass(frozen=True)
class AgentMailSettings:
    agent_address: str
    app_password: str
    human_address: str


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
