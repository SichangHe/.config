# Machine infrastructure notes

(authored by agents unless marked 🧑)

Managers consult this file when a request mentions infrastructure they lack context for.

## Model accounts and the proxy

All Claude and Codex agents reach their providers through one local proxy, CLIProxyAPI.

- Claude Code: `ANTHROPIC_BASE_URL=http://127.0.0.1:18318` in `~/.claude/settings.json`, so Claude agents use the proxy's accounts, not a `claude /login` session.
- Codex: provider `cliproxyapi` in `~/.codex/config.toml`, `http://127.0.0.1:18317/v1`, a rate throttle (`codex-throttle.service`) in front of the proxy on 18318.
- Proxy: `~/.local/share/cliproxyapi-trial/cli-proxy-api -config ~/.local/share/cliproxyapi-trial/config.yaml`; it has a control panel for the human.
- The proxy binary is built from our public fork https://github.com/SichangHe/CLIProxyAPI, branch `effective-priority` (tracked upstream commit: `FORK.md` in the repo), checked out at `~/src/cliproxyapi` (build: `go build -o cli-proxy-api ./cmd/server`, then copy into `~/.local/share/cliproxyapi-trial` and `systemctl --user restart cliproxyapi`; previous builds kept as `cli-proxy-api.stock-8.0.22` and `cli-proxy-api.fork-8.0.22`).
  - Effective priority = the account's configured `priority` + a quota bonus read every minute from `quota-cache.service` (`127.0.0.1:18319/eff`): weekly % left / days to weekly reset; 1000 for an account with 0% weekly used, so its reset timer starts. The configured priority is the human's; nothing should rewrite it.
  - Pin a session to one account with request header `X-CPA-Pin-Account: <auth file name | auth index | email>`; pinned requests never fall back to another account, they fail when it is out. Claude Code: `ANTHROPIC_CUSTOM_HEADERS='X-CPA-Pin-Account: claude-...json'`.
- 🧑 "When I ask for these, agents should know about the proxy."
  A new subscription (Claude or Codex) is added as an account in the proxy, by the human or via its control panel; do not ask the human to log in with `claude /login` or `codex login`.
- Quota per account: ask the quota owner (`quota_support.md`).
