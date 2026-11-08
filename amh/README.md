# Machine infrastructure notes

(authored by agents unless marked 🧑)

Managers consult this file when a request mentions infrastructure they lack context for.

## Model accounts and the proxy

All Claude and Codex agents reach their providers through one local proxy, CLIProxyAPI.

- Claude Code: `ANTHROPIC_BASE_URL=http://127.0.0.1:18318` in `~/.claude/settings.json`, so Claude agents use the proxy's accounts, not a `claude /login` session.
- Codex: provider `cliproxyapi` in `~/.codex/config.toml`, `http://127.0.0.1:18317/v1`, a rate throttle (`codex-throttle.service`) in front of the proxy on 18318.
- Proxy: `~/.local/share/cliproxyapi-trial/cli-proxy-api -config ~/.local/share/cliproxyapi-trial/config.yaml`; it has a control panel for the human.
- 🧑 "When I ask for these, agents should know about the proxy."
  A new subscription (Claude or Codex) is added as an account in the proxy, by the human or via its control panel; do not ask the human to log in with `claude /login` or `codex login`.
- Quota per account: ask the quota owner (`quota_support.md`).
