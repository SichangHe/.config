# task launch helper

`omo_task.py --task-file TASK.md --tmux-session SESSION --workdir DIR --prompt-file PROMPT` creates and links task files, opens a new tmux window with its normal shell, then starts plain Codex by default through `bunx @openai/codex --dangerously-bypass-approvals-and-sandbox`.

`--task-file` is manager-side bookkeeping for `omo_task.py`; keep that path out of worker prompts. Workers report from their own tmux pane with `omo_report.sh --alloc-message-file` and `omo_report.sh --status STATUS --message-file FILE`, without `--task-file`, `--root`, `--manager-target`, or other manual route flags.

Use `--tool pcodx` only for a worker that intentionally needs the PCODX CLI wrapper, its scoped `pcodx_partial_compact` MCP tools, and sidecar ledger artifacts under `/tmp/pcodx-runs`; that path records `tool: pcodx` in task frontmatter.

New task files start with YAML frontmatter containing `version`, `status: running`, `runat`, `tool`, `managerat`, `is_manager`, and `pending_task_items: []`. `--manager-target TARGET`, or the current `OMO_AGENT_TMUX_TARGET`, supplies `managerat`. After launch, record still-open request items with `omo_task_edit.py pending-add` and remove each item with `omo_task_edit.py pending-remove --evidence TEXT` only when it is actually done or cancelled. Normal pending blocks route to `managerat`; `runat` is only for the worker pane and direct-message delivery.

Non-submanager VL worker launches, identified by a `vl_` task filename or the `vl` tmux session, require `--manager-target` so reports and watcher status route to the owning VL submanager. Raw `--codex-flag` MCP server config tokens such as `mcp_servers.*` require explicit `--tool pcodx`, so ordinary new Codex agents do not inherit private partial-compaction MCP registration. The MCP tools provide an auditable partial-compaction ledger; they do not rewrite Codex's hidden native transcript.

`--prompt-file` is passed as Codex's initial prompt argument, not pasted after a startup sleep or injected into an existing Codex TUI input line. VL launches require `--prompt-file` and prepend the narrow VL worker defaults: write the task-local end goal before substantive work, apply the task-relevant reviewer criteria before reporting done, and keep verifier binaries local to verifier-running experiment artifact trees or record exact verifier provenance.

Use `--prelaunch-source SCRIPT` when a worker needs launcher-time environment setup. `omo_task.py` sources that readable shell script inside the worker pane before exporting `OMO_AGENT_TMUX_TARGET` and starting Codex.

Transfer an existing task to a different manager with `omo_task.py --root ROOT --task-file TASK.md --migrate-manager-owner --old-manager-target OLD --new-manager-target NEW`. This operation requires valid frontmatter whose single `managerat` exactly matches `OLD`, atomically changes only that value, and performs no pane, prompt, status, TODO, or launch action. Add `--dry-run` to validate and preview the change without mutating any file or pane.

After sending the shell launch command, it verifies the pane left the shell before writing task/TODO state; if the pane stays at the shell, launch verification fails explicitly. If Codex stops on its update prompt, `omo_task.py` presses Enter to run the update, waits for the restart message, and reruns the original launch command once.

Use `--codex-flag=--model --codex-flag MODEL_NAME` for explicit model selection. Use `gpt-5.6-terra` `medium` by default, `gpt-5.6-terra` `max` for hard tasks, `gpt-5.6-luna` `low` for trivial/minimal tasks, and `gpt-5.6-sol` only when Terra is likely insufficient; `gpt-5.6-sol ultra` is the strongest option but should be rare. `--reasoning-effort EFFORT` passes `--config 'model_reasoning_effort="EFFORT"'`; allowed values are `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`, but routine launches should not go above `max`. Use repeatable `--codex-flag` for extra Codex argv tokens, for example `--codex-flag=--profile --codex-flag deep-review`. Use `--session-id UUID` to start `codex ... resume UUID` or, with `--tool pcodx`, `pcodx ... resume UUID` instead of a fresh session. Pane 0 is implied.
