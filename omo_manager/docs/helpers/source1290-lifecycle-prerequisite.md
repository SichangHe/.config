# Source-1290 lifecycle prerequisite

goal

- prepare only `mem1290_auth.md` for a later fresh close packet
- preserve its blocked lifecycle, two open items, and sole `human pending` TODO custody

inputs

- exact carrier digest `f3d0e041d72ac26cf421b914e9d154a93d8db6304503338f25995119e8d3fc4a` and a fresh TODO digest
- owner-private ownership manifest and exact digest
- exact archived TODO digest and fixed downstream memory, transcription, duplicate-carrier, and interrupted-task records
- exact numeric pane, pane process start, and Codex session
- original owner-private report draft and saved canonical acceptance output
- accepted durable report receipt, publication, and transaction commitment
- immutable completed memory audit
- canonical `~sichangheagent/.config` source HEAD `2e168e0744c976fad65308633e157cbe3942c107` and executed helper-source set
- owner-private terminal receipt path outside the work-log root

carrier state

- require `status: blocked` with `blocked_on: waiting_for_promoted_done_close_recovery_invocation`
- require these two `pending_task_items` in this order
  - `Establish evidence-bound carrier terminalization with an accepted private report and stabilize/authenticate canonical TODO current-row custody through supported tooling; fail closed if prerequisites remain unavailable; do not execute carrier recovery.`
  - `Stabilize and authenticate this canonical carrier task/queue and sole canonical TODO current row; emit exactly one accepted private blocked/terminalization-ready report; generate the bounded ownership manifest bound to stable current state and installed HEAD; stop after preflight evidence or report one supported blocker.`
- bind the ordered list as canonical JSON with SHA-256 `57e13091c5b8ec0a942fdb81da6611c164057e405d81f8224678f7555f7ee5fa`

source observation

- use only `git -C ~sichangheagent/.config rev-parse --verify HEAD`
- require exact output `2e168e0744c976fad65308633e157cbe3942c107`
- reject another source path, the stale `543475ccf538d3b27114cf4f2f3e257b4790ace3` value, or any later observed HEAD

ownership preflight

- after report acceptance, run `omo_source1290_prerequisite.py ownership-preflight --root ROOT --todo-sha256 DIGEST`
- save its canonical JSON output in an owner-private mode `0600` file and pass that file with `--ownership-manifest` and `--ownership-manifest-sha256`
- the manifest contains every unique task row in the exact authenticated `TODO.md`, capped at 512 entries, with its section, declared target, parsed lifecycle target/status, inode identity, mode, owner, bytes, and owner-owned parent directory identity/mode
- only those authoritative indexed tasks are authenticated for ownership; unrelated unindexed historical Markdown, including preserved shared-mode records, is neither read nor changed

contract

- require one `blocked` report with `accepted=true` and manager acknowledgment before any pane input
- run the report subsystem's canonical receipt validator, including all seven side-effect records and manager-acknowledgment transition
- rederive report, receipt, publication, commitment, route, task, TODO, audit, source, membership, post-archive state, and target ownership evidence
- require the manifest to equal a fresh reconstruction of the complete TODO task index under the membership and target locks
- reject an omitted, duplicated, added, removed, aliased, non-regular, non-owner-controlled, writable, malformed, or concurrently changed indexed task, including parent identity, owner, or mode drift
- treat implicit `.0` pane forms as target aliases when proving sole ownership
- require the canonical carrier once under `human pending`
- require the duplicate carrier once under `human pending`
- write a deterministic prepared intent before the first `/status` or interrupt
- exit only the exact live Codex process to its existing ordinary shell
- authenticate the report token, session UUID, unchanged pane, and final terminal capture
- atomically replace the intent with a self-bound `terminalized` receipt
- record `transition: none` with the still-blocked status, blocker, TODO section, ordered items, and item-list digest

retry

- prepared plus the same live Codex pane resumes terminalization, including after one incomplete `Conversation interrupted` marker
- prepared plus the same authenticated shell finishes receipt publication
- terminalized plus the same evidence returns identical receipt bytes
- missing intent never treats a bare exited shell as success
- conflicting state or transaction residue fails closed

post-terminalization

- the exact task and TODO bytes remain unchanged
- `mem1290_auth.md` remains blocked with both open items and remains the sole target owner under `human pending`
- only its authenticated Codex process has exited to the existing ordinary shell
- the `terminalized` receipt proves pane terminalization, not task completion
- later recovery must separately reauthenticate and reconcile the blocked task, items, and TODO custody through supported lifecycle tooling

boundaries

- no work-log, queue, TODO, audit, or report mutation
- no pane kill, relaunch, recovery invocation, mailbox access, or Human mail
- no production action is part of helper installation or testing
