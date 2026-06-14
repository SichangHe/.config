---
name: long-running-autonomy
description: Durable planning and execution loop for explicitly requested long-running autonomous work. Use when the user asks for long-running autonomous mode, a multi-stage unsupervised task, a persistent PLAN file, or continued work across many implementation and verification cycles.
---

# Long Running Autonomy

Use this only when the user explicitly asks for long-running autonomous mode or an equivalent durable multi-cycle run.

## Start

- Create `PLAN-[session-name].md`.
- Record the task, constraints, user instructions, assumptions, dependencies, validation plan, and backup plan.
- Critique the plan before executing it.
- Simplify the plan where possible.

## Loop

- Before implementation, inspect how the codebase already solves similar problems.
- Keep the plan current:
  - delete completed tasks
  - preserve live blockers
  - record changed assumptions
- Use realistic tests, scripts, or checks that match intended behavior.
- Prefer static checks plus behavior checks over trivial mocks.

## Stuck

- Treat repeated failed builds, hanging commands, and circular debugging as a stuck signal.
- Stop broad trial and error.
- Recheck assumptions with smaller tests.
- Reread the plan and replan from first principles.

## Check

- Run the strongest relevant checks before reporting completion.
- Review control flow, edge cases, failure paths, and diff size.
- Use a reviewer subagent when available for non-trivial code or instruction changes.

## Stop

- Continue until no plan TODO remains or a real blocker prevents progress.
- Remove the temporary plan file only after all remaining work is resolved or moved to a durable task record.
- Report the result, changed artifacts, checks, blockers, and remaining human decisions.
