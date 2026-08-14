# draft: plan work with CliGate quota

Status: unapproved draft. Do not treat this file as manager instruction until the Human approves its exact text.

- scope
  - managers planning OpenAI or ChatGPT work on this host through `cligate-canary.service`
  - after approval, applies while the localhost quota endpoint exists or until the Human replaces this guidance
- query
  - refresh and summarize each account
    - `curl -fsS 'http://127.0.0.1:18181/api/quota?refresh=true' | jq '.accounts[] | {account, state, provider_used_percent: .measured.usedPercent, provider_remaining_percent: .measured.remainingPercent, allowed: .measured.allowed, limit_reached: .measured.limitReached, reset_at: .measured.resetAt, counted_tokens_used: .counted.usedTokens, counted_complete: .counted.complete, estimated_tokens_left: .estimated.remainingTokens, estimate_source: .estimated.quotaBudgetTokensSource, uncertainty}'`
  - equivalent checkout CLI
    - `CLIGATE_URL=http://127.0.0.1:18181 node bin/cli.js quota --refresh`
- interpret
  - `measured` values come from current provider status; they are not raw-token limits
  - `counted.usedTokens` includes only input and output tokens in ChatGPT-pool receipts seen by this CliGate instance during the current reset window
  - `counted.complete: false` means the count is only a lower bound; without a configured token budget, tokens left remain unavailable
  - `estimated.remainingTokens` is a rough derived value; `null` means the required denominator is unavailable
  - `state` values other than `ok` contribute no planning capacity
  - dollar values are separate estimates and can remain unavailable when token estimates exist
- plan
  - refresh immediately before assigning substantial work
  - use only `ok` accounts and treat `estimated.remainingTokens` as a relative ceiling, not a provider guarantee
  - keep work below the displayed target token burn rate and preserve the configured safety margin
  - when tokens left are unavailable, plan only from measured status, remaining percentage, and reset time; do not invent a token quota
  - refresh after substantial work or when an account becomes blocked
