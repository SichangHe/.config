# helper environment

`pyproject.toml`, `.python-version`, and `uv.lock` define the manager-helper Python environment.

`omo_manager_setup_watchers.sh` starts pending and email watchers through `uv run --project ~/.config/omo_manager` when `uv` is installed, falling back to direct script execution otherwise. Setup refresh kills old pending watchers and legacy watch-mode stuck watchers for the active root, and old email watchers for this user, before starting replacements. It does not start a stuck watcher.

Watcher logs live under `~/.local/state/omo-manager/` by default: `pending-watch.log` for pending-marker dispatch, agent-problem notices, and stuck handling through `omo_agent_status.py --problems-only`; and `email-watch.log` for mail ingestion. `omo_manager_setup_watchers.sh` prints the exact log paths after restart.

`omo_manager_quiet_check.sh` runs unittest under the same uv project when available. Direct `#!/usr/bin/env python3` helper execution remains supported for existing callers.
