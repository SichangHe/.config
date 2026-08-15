# Main-manager model recovery

`omo_main_manager_model_recovery.py` is the only automated recovery for the main manager's fixed `wl:1` pane. It has no target flag, no live `/model` picker path, and no fresh-manager path. The executable has self-contained `uv` script metadata, so invoke it directly from the manager-helper directory rather than wrapping it in an undeclared Python environment.

It acts only when all of these remain true through the replacement boundary:

- `wl:1.0` is the exact live Codex pane and its process, window, and working directory have not changed
- its current error is exactly the bare ChatGPT-account rejection for `gpt-5.6-sol`, with no other visible error or real typed input
- its one descendant Codex launch still says `gpt-5.6-sol` and has one valid reasoning effort
- that launch retains the exact main-manager environment: `OMO_AGENT_TMUX_TARGET`, `OMO_MANAGER_TMUX_TARGET`, `OMO_WORK_LOGS_ROOT`, and a private `OMO_MANAGER_STATE_DIR`
- the supplied source is an owner-private direct file under `ROOT/manager_mail`, and a separate owner-private envelope is a direct file under `ROOT`; the envelope contains exactly one `<human_instruction authoritative="true" source="manager_mail/FILE:START-END">` block whose body is byte-for-byte the selected source lines
- those selected human-instruction lines consist only of one exact positive grant for the requested model; plain mail text, routing context, denials, and `<manager_delegation>` are rejected
- an isolated `codex exec --ephemeral` request using the requested model and preserved effort returns the exact schema result
- a server-guarded `/status` query exposes one valid current Codex session UUID

The availability probe runs in a temporary empty directory with a read-only sandbox, ignored user config and rules, and no persisted session. It proves access at that instant; a later account change still fails after respawn instead of causing a retry.

The helper's `/status` paste and each Enter are inside a tmux-server condition over the pinned pane, window, canonical target, PID, and command. A mismatch rejects the input before it reaches the pane. The helper writes a new owner-private handoff record before calling the guarded `respawn-pane` routine from `omo_codex_start.py`. The resumed command preserves the exact pane, working directory, session UUID, manager environment, and reasoning effort while changing only the requested model. It passes both the pinned `--cd` directory and the nonpersistent `tui.resume_cwd="current"` override, so it does not drive a working-directory picker. It then proves the new pane process, model/effort, manager environment, and resumed UUID. A post-replacement problem is recorded as `completion-unknown`; the helper never retries automatically.

Use an actual authoritative email source whose selected lines contain only this exact positive grant, wrapped without any surrounding content in a separate envelope. `FILE`, `START`, and `END` must match the command arguments exactly, and `MODEL` must equal `--model`:

```text
<human_instruction authoritative="true" source="manager_mail/FILE:START-END">
main-manager-model-recovery: target=wl:1 action=resume-same-pane model=MODEL replacement-pane=forbidden
</human_instruction>
```

`--dry-run` performs only noninteractive gates, including the isolated availability probe and final authority/binding checks. It does not query `/status`, write a handoff record, or alter `wl:1`.
