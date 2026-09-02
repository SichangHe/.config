#!/usr/bin/env python3
"""Resolve the manager process environment and local.env once."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


def load_local_env() -> dict[str, str]:
    env = dict(os.environ)
    local_env = Path(env.get("OMO_MANAGER_LOCAL_ENV", Path.home() / ".config" / "omo_manager" / "local.env"))
    if not local_env.is_file():
        return env
    loaded = subprocess.run(["bash", "-c", 'set -a; source "$1"; env -0', "bash", str(local_env)], capture_output=True, timeout=10, check=False)
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


CONFIGURED_ENV = load_local_env()


def manager_state_dir(environment: Mapping[str, str] = CONFIGURED_ENV) -> Path:
    return Path(environment.get("OMO_MANAGER_STATE_DIR", Path(environment.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))


def external_task_registry_dir(environment: Mapping[str, str] = CONFIGURED_ENV) -> Path:
    return Path(environment.get("OMO_EXTERNAL_TASK_REGISTRY", manager_state_dir(environment) / "external-task-registrations"))
