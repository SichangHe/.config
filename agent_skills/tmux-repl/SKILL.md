---
name: tmux-repl
description: Use when a task needs controlled interaction with a long-running REPL, shell, server, notebook, or interactive process.
---

Use tmux as the control surface for any long-running interactive process.

Create a named tmux session or window for the process.
Start the process inside tmux.
Record the tmux target in task notes when the run is non-trivial.

Send commands with `tmux send-keys` ending in Enter.
Print unique markers before commands that produce important output.
Capture pane output to a temp file.
Read only bounded line ranges around the relevant marker.
Avoid reading the full pane when the process has long history.

Leave useful long-running processes alive only when the user or task needs them.
Kill unused sessions created by the agent.
Report the tmux target and any live process the user may need.
