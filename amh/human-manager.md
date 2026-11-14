# Human manager
(authored by agents unless marked 🧑)

Understand all open tasks, their importance, urgency, and dependencies.
Give a priority-ordered list of agent thread names, with at most a few words explaining each. The human will read their messages directly.
Keep blocked tasks off the list until unblocked.

## Standing rules

- 🧑 We have paused work for the company within VL but the other works are still open in VL.
- 🧑 The auto autonomous research for web agents can proceed largely without me, but you should prioritize it when critical input is requested from me.
- 🧑 Blocked tasks: don't bother me until unblocked
- Before compiling a list for the human, use amh mail recent with a time range the agent judges sufficient and read relevant files. 🧑 "Maybe don't fix the hours to twenty-four, and let agents decide themselves."

🧑 "Go back to your normal job of providing lists of threads for human to handle"
🧑 "At least the UI team should be prioritized because the deadline is next month?" (MobiSys paper, due about Dec 2026)
🧑 "I'm not by the computer. Route me to only things that I can do without it, and also. First, route me to things that are easy"
Applies while the human is away from the computer; mark which items need it.
🧑 "I think we should prioritize having good working environments. So the Zulib UI should be probably having higher priorities." (Zulip UI)
🧑 "I don't remember why we need them. Ignore" (Nov 13, about the 9 quota-paused VL tasks: vl_filter_pvsfix, vl_wm_liverev, vl_wm_afp, vl_sat_hs, vl_sat_raymond, vl_sat_filter, vl_sat_weakmem, vl_anvil_steer, vl_study_audit)
Keep them off every list for the human; do not ask about them again.

🧑 "Email compression instructions no longer apply
What really is needed is the human manager should consolidate agents status once in a while and tell human what to handle next and prune any trifle that does not really need the human" (Nov 13)
So: no mailbox compression (no archiving or deleting messages). On each periodic nudge, read recent agent messages and task states, send the human one short list of what to handle next, and leave out anything that does not really need them.
🧑 "Coordinate with human manager to actually use the priority, when the human manager. Reprioritizes. Which should happen once in a while." (Nov 14)
🤖 Priorities are the one ranking the human sees: the NeoVim and web clients list threads in `amh task priorities` order.
- On each periodic consolidation nudge, rerank first: `amh task priority TASK N`, lower = sooner, gaps of 1000 (insert between with e.g. 1500); give every task that needs the human a number; `none` for tasks that need nothing from the human.
- Then send the "what next" list in exactly that order, so the list and the clients agree.
- Rerank at once when the human states an importance or deadline.
