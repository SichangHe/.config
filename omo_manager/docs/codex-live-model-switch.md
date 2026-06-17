# codex live model switch

Use this only for a running Codex pane that must change model during an in-progress task.

Supported path:
- confirm the target tmux pane is the live Codex session
- interrupt the running task with tmux `C-c`
- open the interactive model picker from the live Codex UI
- use the picker to choose the model and reasoning level
- resume the Codex task after the pane shows the new model

Why this is documented instead of automated:
- `/model` is rejected while a task is in progress unless the interactive picker path is used
- picker contents and key order vary with the live Codex UI
- supported model ids vary by account and environment, so a script would need brittle UI scraping and retries

Fallback path:
- if `/model` is rejected, stop after the interrupt and keep the current supported model running
- if the picker does not match expectations, do not try to force the switch with relaunch-time `--model`
- when a different model is still required, re-check support for that exact model before any relaunch and prefer a fresh launch over live mutation
