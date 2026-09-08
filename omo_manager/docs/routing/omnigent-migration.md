# gradual OmniGent migration

authority

- `manager_mail/85c5dff58359-1513.txt:3-8`
- “move to Omnigent bit by bit”
- “add Omnigent session as a valid runat while keeping backwards compatibility for tmux somehow”

goal

- keep current task records as lifecycle authority
- replace one runtime-facing capability at a time
- retain tmux until each OmniGent adapter has equivalent evidence and recovery

target contract

- tmux `SESSION:WINDOW[.PANE]`
  - unchanged
- OmniGent `omnigent://SESSION_ID`
  - session id uses ASCII letters, digits, `.`, `_`, or `-`
  - URI-like syntax cannot overlap tmux syntax
  - requires `tool: omnigent`
  - example: `omnigent:123` remains tmux
  - example: `omnigent://019f0000-0000-7000-8000-000000000123` is OmniGent
- `managerat`
  - remains tmux-only in this slice

migration slices

- represent
  - parse and classify an OmniGent session `runat`
  - status reports an unsupported adapter without pane inspection
  - task-status mutation and tmux delivery reject the target
- observe
  - resolve OmniGent liveness and status without pane inspection
  - compare results with existing task status in shadow mode
- deliver
  - route prompts, agent messages, pending notices, and reports by target kind
  - preserve stable ids and replay receipts
- control
  - add explicit OmniGent start, attach, stop, and recovery adapters
  - never infer lifecycle completion from runtime output
- cut over
  - switch one helper capability after parity and rollback checks
  - remove its tmux dependency only after no active task needs it

current limit

- an OmniGent-backed record can be represented
- launch, stop, pane status, task-status mutation, and delivery remain tmux-only
- this slice creates or changes no live session
