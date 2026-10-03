"""Machine-local settings, read from the environment and then from `local.env`."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

LOCAL_ENV = Path(__file__).resolve().parent.parent / "local.env"
OMNIGENT_PREFIX = "omnigent://"


@dataclass(frozen=True)
class Config:
    values: dict[str, str]

    def get(self, key: str, default: str = "") -> str:
        return os.environ.get(key) or self.values.get(key, default)

    @property
    def root(self) -> Path:
        """The work-log root holding task files, `TODO.md`, and stored human mail."""
        return Path(self.get("OMO_WORK_LOGS_ROOT", str(Path.home() / "work_logs")))

    @property
    def state_dir(self) -> Path:
        return Path(self.get("OMO_MANAGER_STATE_DIR", str(Path.home() / ".local/state/omo-manager")))

    @property
    def mail_dir(self) -> Path:
        return self.root / "manager_mail"

    def own_address(self) -> str:
        """Where the calling agent runs: its Omnigent session, else its tmux window."""
        if session := os.environ.get("OMNIGENT_RUNNER_PRIMARY_SESSION_ID"):
            return OMNIGENT_PREFIX + session
        if pane := os.environ.get("TMUX_PANE"):
            env = {key: value for key, value in os.environ.items() if key != "TMUX"}
            shown = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{session_name}:#{window_index}"], capture_output=True, text=True, check=False, env=env)
            if shown.returncode == 0:
                return shown.stdout.strip()
        raise SystemExit("amh: cannot tell which agent is calling; pass --task-file NAME.md")


def load() -> Config:
    """Parse `export KEY="value"` lines, expanding `$VAR` references to earlier keys and the environment."""
    values: dict[str, str] = {}
    text = Path(os.environ.get("OMO_MANAGER_LOCAL_ENV", LOCAL_ENV)).read_text(encoding="utf-8")
    for key, raw in re.findall(r'^export (\w+)="(.*)"$', text, re.MULTILINE):
        expanded = re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: os.environ.get(m[1]) or values.get(m[1]) or m[2], raw)
        values[key] = re.sub(r"\$(\w+)", lambda m: os.environ.get(m[1]) or values.get(m[1], ""), expanded)
    return Config(values)
