---
name: long-running-autonomy
description: Use when the human explicitly asks for long-running autonomous mode, durable multi-stage work, a persistent PLAN file, or continued work across many implementation and verification cycles.
---

Use this only when the user explicitly asks for long-running autonomous mode or an equivalent durable multi-cycle run.

Create `PLAN-[session-name].md`.
Record the task, constraints, user instructions, assumptions, dependencies, validation plan, and backup plan.
Critique the plan before executing it.
Simplify the plan where possible.

Before implementation, inspect how the codebase already solves similar problems.
Keep the plan current:

- Delete completed tasks.
- Preserve live blockers.
- Record changed assumptions.

Use realistic tests, scripts, or checks that match intended behavior.
Prefer static checks plus behavior checks over trivial mocks.

Stuck signal: repeated failed builds, hanging commands, or circular debugging.
Stop broad trial and error.
Recheck assumptions with smaller tests.
Reread the plan and replan from first principles.

Before reporting completion, run the strongest relevant checks.
Review control flow, edge cases, failure paths, and diff size.
Use a reviewer subagent when available for non-trivial code or instruction changes.

Continue until no plan TODO remains or a real blocker prevents progress.
Remove the temporary plan file only after all remaining work is resolved or moved to a durable task record.
Report the result, changed artifacts, checks, blockers, and remaining human decisions.
