# Codex worker rotation audit

- replacement checkpoint
  - accepts tmux commands `bun`, `bunx`, and `codex`
  - records the changed process before startup and UUID verification
  - a wrapper or unsupported command remains uncheckpointed
- reconciliation eligibility
  - requires the replacement checkpoint
  - requires the exact UUID-capture failure kind
  - requires the inode-bound eligibility digest written by finalization
- old pre-checkpoint failures
  - remain immutable and unreconcilable
  - a matching path, content digest, or added xattr cannot supply missing causal evidence
