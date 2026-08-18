# experiment record helper

purpose

- `omo_experiment_record.py` packages files supplied by its caller
- it does not launch agents or experiments
- it preserves only explicitly supplied files and does not establish global transcript completeness

interface

- required
  - initially absent `--output-dir` whose parent already exists
  - one or more repeatable `--transcript` files
  - one `--prompt` file
  - timezone-aware ISO 8601 `--started-at` and `--ended-at`
- optional
  - repeatable `--input` attachments
- example
  - `omo_experiment_record.py --output-dir record --transcript turns.jsonl --prompt prompt.txt --input context.txt --started-at 2026-08-13T12:00:00-07:00 --ended-at 2026-08-13T12:30:00-07:00`

record

- `transcripts/` contains verbatim transcript bytes
- `attachments/prompt/` and `attachments/inputs/` contain detached exact copies
- `summary.txt` is the concise human view
- `manifest.json` uses `omo-experiment-record/v1`
  - caller-supplied timestamps and derived nonnegative elapsed seconds
  - source and destination SHA-256 plus byte counts
  - token metrics only from valid Codex JSONL `payload.info.total_token_usage` records
    - each counted record needs a timezone-aware ISO 8601 event timestamp
  - `unavailable` when no supported token record is present
  - `unavailable` when a supported token record lacks a usable timestamp, rather than guessing counter order

safety

- source files must be non-symlink regular files and remain unchanged through validation
- source copies, hashes, and validation are streamed without retaining complete transcripts in memory
- duplicate supplied basenames are rejected
- the existing output parent must be owned by the caller's effective user and grant no group or other write permissions
- the output parent must contain no symlink path component
  - its directory descriptor is held from initial validation through staging and publication
  - its path identity is rechecked before and after publication; a failed post-publication check rolls the record back
- every artifact file is mode `0600`
- a private sibling staging directory is fully validated and synced before atomic no-replace publication
- the output parent is synced after publication
- an existing destination is never overwritten
- atomic publication requires Linux `renameat2` and `/proc/self/fd` support from the host filesystem
