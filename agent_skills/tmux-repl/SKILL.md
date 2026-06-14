---
name: tmux-repl
description: Use when a task needs controlled interaction with a long-running REPL, shell, server, notebook, or interactive process.
---

To spawn and continuously interact w/ long-running REPL, you MUST use tmux

Create a tmux session and start the REPL there

To read REPL output, pipe `tmux capture-pane` to a file
Never directly read the entire REPL output screen to avoid context pollution
Print unique markers to help yourself find relevant new output
To run commands in the REPL, `tmux send-keys` ending with Enter

Be very careful to kill all unused tmux session you create to avoid leaking resources

Additional skill procedure:

- Record the tmux target in task notes when the run is non-trivial.
- Read only bounded line ranges around the relevant marker.
- Leave useful long-running processes alive only when the user or task needs them.
- Report the tmux target and any live process the user may need.
