# memory shared-target cancellation

goal

- cancel only `memory_research_mgr.md` under exact Human Source-1290
- preserve `transcription_sw.md` and shared target `wl:32`

contract

- `omo_task_status.py --cancel-shared-target`
  - authenticates exact source and sole authoritative envelope
  - binds complete memory, transcription, and TODO bytes
  - requires exactly those two active `wl:32` records
  - preserves the cancelled ordered queue in a new owner-private audit
  - clears only the memory queue and blocker
  - sets only memory status to `done`
  - moves only the memory TODO row to `previous`
  - accesses no tmux or mail API

failure policy

- reject wrong authority, task, target, protected task, or owner set
- reject task, queue, TODO, membership, or protected-record drift
- reject active descendants, malformed rows, unsafe audit paths, and rollback failure
- never infer cancellation from an email receipt or task prose
