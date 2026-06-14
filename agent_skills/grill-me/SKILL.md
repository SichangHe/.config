---
name: grill-me
description: Relentless planning interview for turning vague plans, designs, research directions, implementation strategies, or project proposals into shared understanding before execution. Use when the user asks to be grilled, pressure-tested, interviewed about a plan, or when unresolved decisions and dependencies must be exposed before work starts.
---

# Grill Me

Use the source instruction: "Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree resolving dependencies between decisions one by one."

## Method

- Keep the user in decision mode until the plan is concrete enough to execute.
- Ask one focused question at a time unless the user explicitly asks for a batch.
- Prefer questions that expose dependencies, hidden assumptions, irreversible choices, missing constraints, and success criteria.
- Walk the design tree depth-first:
  - name the current branch
  - ask what decision blocks progress
  - record the answer as a decision, assumption, constraint, or open question
  - follow any dependency before moving sideways
- Challenge weak answers directly:
  - ask for examples when language is abstract
  - ask for tradeoffs when the answer only states a preference
  - ask for failure modes when the answer only describes success
  - ask for ownership and validation when the answer implies work
- Stop only when:
  - goals, non-goals, constraints, dependencies, risks, validation, and next action are explicit
  - remaining unknowns are named and acceptable
  - the user agrees the shared understanding is sufficient

## Output

- Keep a compact decision log when the exchange is long.
- Separate fact, assumption, inference, decision, and open question.
- Summarize the final plan only after the interview has resolved the important branches.
