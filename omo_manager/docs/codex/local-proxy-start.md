# Local proxy startup

(authored by agents unless marked 🧑)

- 🧑 "Broken things include: Codex agent spawning"
- default launcher uses `bunx @openai/codex@latest --no-daemon` and existing login/config
- Codex 0.159.2 discovers the saved ChatGPT workspace before startup
  - the rotating local proxy can return a different pool account's workspaces
  - upstream discovery also requires HTTPS workspace origins
- explicit fresh-launch option `--local-proxy-url URL`
  - requires an unauthenticated HTTP loopback Responses endpoint, explicit port, and `/backend-api/codex` or `/v1` path
  - uses per-process `omo_local_proxy` provider with `wire_api="responses"` and `requires_openai_auth=false`
  - disables the alternate screen so the launch marker stays visible to startup verification
  - the proxy owns upstream authentication and account rotation; the client does not claim to be a pool member's ChatGPT workspace
  - does not edit saved auth, user config, the package default, or the running proxy
  - rejects credentials, remote hosts, query strings, fragments, resume, rotation, and recovery
  - intended for fresh tmux Codex workers, not PCODX or migrating existing threads
- local canary example
  - add `--codex-package @openai/codex@latest --local-proxy-url http://localhost:18181/backend-api/codex` to an otherwise valid fresh `omo_codex_start.py` command
- live regression opt-in
  - `OMO_LIVE_CODEX_HANDOFF_TEST=1 OMO_LIVE_CODEX_PACKAGE=@openai/codex@latest OMO_LIVE_CODEX_PROXY_URL=http://localhost:18181/backend-api/codex`
  - disposable handoff test uses an isolated Codex home and temporary project, then exercises the launcher command, empty composer, session UUID query, prompt submission, cleared prompt guard, and actual model reply through the proxy
  - ordinary `/status` lookup waits for its exact command to render before the bounded retry deadline; retained `/status` is not unrelated input
