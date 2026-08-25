# manager-mail compression

These instructions govern future cleanup of manager-sent mail in the Human's Inbox. Use them for both threshold-triggered and Human-requested compression runs.

## outcome

- minimize the total accepted manager-sent Inbox to the smallest truthful human-readable set; aim near 20 and never stop merely because the count reached 30
- use `snapshot`'s configured manager-mail boundary as the count denominator: every accepted Inbox message from the configured manager sender to the Human or accepted legacy self-addressed route; include overviews, retained sources, protected reports, and later arrivals
- leave no more than 30 total accepted manager-sent Inbox messages unless preserving independently useful content makes that impossible
- represent each actual task with one concise, self-contained overview
- retain each distinct current question or decision that the Human can act on independently
- retain protected recurring reports separately from task overviews only when they are accepted by the configured manager-mail boundary
- identify a protected recurring series by its configured sender/recipient boundary and recurring report subject; require a non-empty complete body and expected report heading, order instances by numeric Gmail message identity, and retain the greatest verified instance; a greatest partial, conflicting, or ambiguous instance blocks cleanup of that series
- PB news, PB stock watch, and PB urgent mail are excluded from this manager-mail compression workflow; handle them through their PB-specific digest paths instead of counting or moving them here
- move only reviewed sources that a retained message or verified replacement fully supersedes to recoverable Gmail Trash

## prepare a current view

1. Run `snapshot`, `identity-preflight`, and `unread-summary` in read-only mode against the configured mailbox.
2. Freeze an explicit starting source set from that live view. Treat a count threshold only as a reason to review mail.
3. Inspect the complete message and thread context for every proposed source. Use current task and decision state only to determine what remains useful to the Human.
4. Leave later arrivals outside the frozen set until they are separately inspected. Thread membership alone does not assign a later arrival to a group. Retain a new independent question or decision. Add every other relevant arrival to a newly frozen explicit source set and repeat binding and review, even when its facts are already present and the overview text stays unchanged.

## design the human view

1. Group messages only when they belong to one authoritative task identity and can share one current outcome, one set of limits, and one next Human action without requiring separate replies. A task group may span internal work streams, historical routes, threads, or repositories. When one source covers several tasks, bind it to every covered task and do not move it until each task has its own reviewed retained message or replacement. Keep tasks separate even when their subjects or implementation overlap.
2. For each group, draft one high-level overview containing only:
   - current outcome or state
   - facts the Human needs now
   - unresolved decision or next action, if any
   - material limits or uncertainty
3. Omit implementation history, routine acknowledgements, agent bookkeeping, paths, identifiers, hashes, and completed details unless the Human needs them to decide or act.
4. Prefer a new overview over retaining the newest message when the newest message is partial, detailed, stale, or not self-contained.
5. Retain a full memo only when the Human still needs its complete technical content and a concise overview cannot preserve that value. A self-contained retained full memo is the task's current message; do not add a duplicate overview. If separate sources for that task contain additional current facts, the replacement overview may point to the retained memo and include only those additional facts; record the resulting two-message full-memo exception.
6. A question is independently actionable when the Human can answer it without answering the group's other questions and that answer can change work independently. Keep such questions separate. Put dependent subquestions and ordinary next steps inside the area's overview.
7. Revise grouping and summaries until no remaining pair can be truthfully combined under the grouping test. If the resulting total still exceeds 30, retain the useful content, move no uncertain source, and report the exact consolidation blocker.

## independent review

Before sending or moving mail, give a distinct reviewer the current read-only view, complete inspected context, proposed groups, summaries, retained messages, explicit source bindings, current route for each replacement, and later-arrival handling. The reviewer must confirm:

- every useful fact, question, decision, limit, and uncertainty is present once
- each overview is self-contained and useful without reading its sources
- every selected source is fully superseded
- retained full memos and separate questions genuinely need to remain separate
- replacement routes and any route transitions are explicit and correct
- the projected final Inbox is the smallest truthful set and is at most 30 messages, or the review names the exact useful content that prevents reaching 30

Resolve every material review issue and repeat the review on changed text or bindings.

## replace, verify, and move

1. Inspect each approved explicit source set with `inspect-explicit` and bind its source UIDVALIDITY, source identities, thread context, and original sender target.
2. Resolve the authoritative current sender target from current task records and the manager hierarchy. Use the task's documented current owner and have the reviewer approve every transition from historical targets. A missing, conflicting, or inferred-only target blocks sending and movement for that group.
3. Send each approved overview with a unique subject and the independently reviewed authoritative sender target. When an existing retained message already is the approved self-contained current message, send nothing; locate that exact retained message uniquely and use its identity as the superseding message.
4. Locate the exact replacement uniquely with `locate-replacement`. Do not send a duplicate while delivery lookup is pending.
5. Rebuild the read-only view and rerun `inspect-explicit` immediately before moving a group. Any source identity change requires a new frozen source set, complete regrouping, and repeated review. If the restaged facts still match the approved overview, reuse the uniquely located existing replacement. If any fact changes, freeze the already-sent overview as another source and apply the revised-replacement procedure in the next step.
6. Inspect each later thread arrival by content. If it is independent, retain it and have the reviewer confirm that the existing replacement still fully supersedes only the restaged original sources. If its relevant facts are already present, add it to the newly frozen explicit source set and have the reviewer reconfirm the unchanged replacement and all bindings; retain it with an exact reason or move it only when that replacement fully supersedes it. If any later arrival or source drift changes the overview, freeze the already-sent overview as another source, draft one revised overview, review the revised source set and text, locate the revised replacement uniquely, and move the stale overview only when it is explicitly bound and fully superseded. Never send the same text twice.
7. Run `trash-explicit` with the reviewed task/group identities, source bindings, context bindings, replacement identities, route resolution when needed, source UIDVALIDITY, and distinct preparer and reviewer identities.
8. Move only explicitly bound, fully superseded sources to `[Gmail]/Trash`.
9. Finish and verify one reviewed group at a time so drift in one group does not affect another.

## finish

Build a final read-only `snapshot` using the same configured boundary and confirm:

- the smallest truthful set remains and the total accepted manager-sent Inbox count is at most 30, or an exact preservation blocker is reported
- every task has one current overview
- every independent current question or decision remains visible
- protected recurring reports accepted by the manager-mail boundary remain present
- only approved superseded sources moved to recoverable Trash

Treat a retained self-contained full memo as its task's current message when it already provides the needed human view. Count each protected current report separately from task overviews.

Report the starting and final total accepted manager-sent Inbox counts, overview count, protected recurring count, separately retained question/decision count, full-memo exceptions, moved-to-Trash count, later-arrival handling, and unresolved blockers.

Use the configured private mailbox path. Keep message bodies and identifiers out of reports. Do not mark messages read as cleanup. Do not expunge, permanently delete, mutate Gmail All Mail, or move unreviewed mail.
