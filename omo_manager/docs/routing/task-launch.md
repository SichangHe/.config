# task launch helper

`omo_task.py --task-file TASK.md --tmux-session SESSION --workdir DIR --prompt-file PROMPT` creates and links task files, opens a new tmux window with its normal shell, then starts plain Codex by default through `bunx @openai/codex --dangerously-bypass-approvals-and-sandbox`.

Use `--tool pcodx` only for a worker that intentionally needs the PCODX CLI wrapper, its scoped `pcodx_partial_compact` MCP tools, and sidecar ledger artifacts under `/tmp/pcodx-runs`; that path records `runat: SESSION:WINDOW pcodx`.

New task files also get `(above are pending task items)` after the prompt body. Bullets above that marker are human request items; remove each bullet only when that item is actually done. `--manager-target TARGET` writes `managerat: TARGET` after `runat:` for submanager-owned tasks; omit it only for main-manager-owned tasks.

Non-submanager VL worker launches, identified by a `vl_` task filename or the `vl` tmux session, require `--manager-target` so reports and watcher status route to the owning VL submanager. Raw `--codex-flag` MCP server config tokens such as `mcp_servers.*` require explicit `--tool pcodx`, so ordinary new Codex agents do not inherit private partial-compaction MCP registration. The MCP tools provide an auditable partial-compaction ledger; they do not rewrite Codex's hidden native transcript.

`--prompt-file` is passed as Codex's initial prompt argument, not pasted after a startup sleep or injected into an existing Codex TUI input line. VL launches require `--prompt-file` and prepend the narrow VL worker defaults: write the task-local end goal before substantive work, apply the task-relevant reviewer criteria before reporting done, and keep verifier binaries local to verifier-running experiment artifact trees or record exact verifier provenance.

VL experiment and rerun launches matching `vl_*_exp_*.md` or `vl_*_rerun_*.md` are preflight-gated automatically when started with `--workdir`; pass `--vl-preflight-verus PATH_TO_VERUS --vl-preflight-artifact-root ARTIFACT_ROOT` to bind the intended verifier and evidence root. The gate runs before the worker starts, writes `preflight.txt` under the artifact root, exports `VLH`, `VERUS`, `VL_EXPERIMENT_ARTIFACT_ROOT`, and a `PATH` containing the verified helper directory to the worker, and checks intended `vlh` on `PATH`, `vlh help`, Verus executable version/provenance, and GPT-backed OpenRouter absence.

After sending the shell launch command, it verifies the pane left the shell before writing task/TODO state; if the pane stays at the shell, launch verification fails explicitly.

Use `--reasoning-effort xhigh` to pass `--config 'model_reasoning_effort="xhigh"'`; allowed values are `low`, `medium`, `high`, and `xhigh`. Use repeatable `--codex-flag` for extra Codex argv tokens, for example `--codex-flag=--profile --codex-flag deep-review`. Use `--session-id UUID` to start `codex ... resume UUID` or, with `--tool pcodx`, `pcodx ... resume UUID` instead of a fresh session. Pane 0 is implied.
