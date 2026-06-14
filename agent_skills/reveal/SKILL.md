---
name: reveal
description: Use when asked to reveal problems, do a skeptical architecture review, improve codebase architecture, or identify related code that should become a deep module with a simple interface.
---

Reveal the structure that ordinary diff review misses.

Use this for deep critique, architecture improvement, design risk discovery, or review beyond local syntax.
Keep ordinary reviewer agents small; they should reference this skill when a reveal-level review is requested instead of duplicating this workflow.

Reconstruct the current design from source:

- Entry points.
- Data ownership.
- State transitions.
- Boundaries between modules.
- Shared assumptions.
- Failure and recovery paths.

Identify related code before proposing a change:

- Code that mutates the same state.
- Code that encodes the same concept.
- Code that validates or serializes the same data.
- Tests, docs, scripts, and configs that define the behavior.

Name the smallest architecture problem that explains the symptoms.
Prefer a deep module when related code is scattered:

- Put the hard policy, state, or protocol behind one narrow interface.
- Keep callers simple.
- Make ownership explicit.
- Remove duplicate partial implementations.

Keep the interface boring: direct inputs and outputs, explicit expected errors, no speculative extension points, and no hidden global state.
Validate the proposed shape by showing how callers become simpler, naming regression checks, and naming migration risk.

Lead with the highest-risk finding.
Separate fact, inference, and recommendation.
When suggesting a refactor, give the target boundary and the first safe step.
Ask the human for judgment when the decision depends on taste, product direction, or future plans.
