# helper environment

`pyproject.toml`, `.python-version`, and `uv.lock` define the manager-helper Python environment.

`omo_manager_setup_watchers.sh` starts pending, stuck, and email watchers through `uv run --project ~/.config/omo_manager` when `uv` is installed, falling back to direct script execution otherwise. Setup refresh kills old pending watchers for the active root and old email watchers for this user before starting replacements, so stale roots cannot race the active inbox consumer.

Watcher logs live under `~/.local/state/omo-manager/` by default: `pending-watch.log` for pending-marker dispatch and agent-problem notices, `stuck-watch.log` for stuck-pane checks, and `email-watch.log` for mail ingestion. `omo_manager_setup_watchers.sh` prints the exact log paths after restart.

`omo_manager_quiet_check.sh` runs unittest under the same uv project when available. Direct `#!/usr/bin/env python3` helper execution remains supported for existing callers.
