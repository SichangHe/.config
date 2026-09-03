# Source-1290 lifecycle prerequisite

goal

- prepare only `mem1290_auth.md` for a later fresh close packet
- preserve its exact shell pane and `current` TODO custody

inputs

- exact carrier and TODO digests
- owner-private ownership manifest and exact digest
- exact archived TODO digest and fixed downstream memory, transcription, duplicate-carrier, and interrupted-task records
- exact numeric pane, pane process start, and Codex session
- original owner-private report draft and saved canonical acceptance output
- accepted durable report receipt, publication, and transaction commitment
- immutable completed memory audit
- exact `.config` source HEAD and executed helper-source set
- owner-private terminal receipt path outside the work-log root

ownership preflight

- after report acceptance, run `omo_source1290_prerequisite.py ownership-preflight --root ROOT --todo-sha256 DIGEST`
- save its canonical JSON output in an owner-private mode `0600` file and pass that file with `--ownership-manifest` and `--ownership-manifest-sha256`
- the manifest contains every unique task row in the exact authenticated `TODO.md`, capped at 512 entries, with its section, declared target, parsed lifecycle target/status, inode identity, mode, owner, bytes, and owner-owned parent directory identity/mode
- only those authoritative indexed tasks are authenticated for ownership; unrelated unindexed historical Markdown, including preserved shared-mode records, is neither read nor changed

contract

- require `accepted=true` and manager acknowledgment before any pane input
- run the report subsystem's canonical receipt validator, including all seven side-effect records and manager-acknowledgment transition
- rederive report, receipt, publication, commitment, route, task, TODO, audit, source, membership, post-archive state, and target ownership evidence
- require the manifest to equal a fresh reconstruction of the complete TODO task index under the membership and target locks
- reject an omitted, duplicated, added, removed, aliased, non-regular, non-owner-controlled, writable, malformed, or concurrently changed indexed task, including parent identity, owner, or mode drift
- treat implicit `.0` pane forms as target aliases when proving sole ownership
- require the canonical carrier once under `current`
- require the duplicate carrier once under `human pending`
- write a deterministic prepared intent before the first `/status` or interrupt
- exit only the exact live Codex process to its existing ordinary shell
- authenticate the report token, session UUID, unchanged pane, and final terminal capture
- atomically replace the intent with a self-bound completed receipt

retry

- prepared plus the same live Codex pane resumes terminalization, including after one incomplete `Conversation interrupted` marker
- prepared plus the same authenticated shell finishes receipt publication
- completed plus the same evidence returns identical receipt bytes
- missing intent never treats a bare exited shell as success
- conflicting state or transaction residue fails closed

boundaries

- no work-log, queue, TODO, audit, or report mutation
- no pane kill, relaunch, recovery invocation, mailbox access, or Human mail
- no production action is part of helper installation or testing
