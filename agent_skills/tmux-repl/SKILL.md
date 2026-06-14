---
name: tmux-repl
description: Controlled workflow for long-running REPLs, shells, servers, notebooks, and interactive programs. Use when a task needs ongoing interaction with a process that should stay alive while the agent sends commands and reads bounded output.
---

# tmux REPL

Use tmux as the control surface for any long-running interactive process.

## Setup

- Create a named tmux session or window for the REPL.
- Start the REPL inside tmux.
- Record the tmux target in the task notes when the run is non-trivial.

## Interaction

- Send commands with `tmux send-keys` ending in Enter.
- Print unique markers before commands that produce important output.
- Capture pane output to a temp file.
- Read only bounded line ranges around the relevant marker.
- Avoid reading the full pane when the process has long history.

## Cleanup

- Leave useful long-running processes alive only when the user or task needs them.
- Kill unused sessions created by the agent.
- Report the tmux target and any live process the user may need.
