---
name: reveal
description: Deep review workflow for exposing hidden design risks, unclear abstractions, brittle control flow, missing observability, and architecture improvements. Use when asked to reveal problems, do a skeptical architecture review, improve codebase architecture, or identify related code that should become a deep module with a simple interface.
---

# Reveal

Reveal the structure that ordinary diff review misses.

## Scope

- Use this for deep critique, architecture improvement, design risk discovery, or review beyond local syntax.
- Keep ordinary reviewer agents small; they should reference this skill when a reveal-level review is requested instead of duplicating this workflow.

## Workflow

- Reconstruct the current design from source:
  - entry points
  - data ownership
  - state transitions
  - boundaries between modules
  - shared assumptions
  - failure and recovery paths
- Identify related code before proposing a change:
  - code that mutates the same state
  - code that encodes the same concept
  - code that validates or serializes the same data
  - tests, docs, scripts, and configs that define the behavior
- Name the smallest architecture problem that explains the symptoms.
- Prefer a deep module when related code is scattered:
  - put the hard policy, state, or protocol behind one narrow interface
  - keep callers simple
  - make ownership explicit
  - remove duplicate partial implementations
- Keep the interface boring:
  - direct inputs and outputs
  - explicit expected errors
  - no speculative extension points
  - no hidden global state
- Validate the proposed shape:
  - show how current callers become simpler
  - name tests or checks that would catch regressions
  - name any migration or compatibility risk

## Output

- Lead with the highest-risk finding.
- Separate fact, inference, and recommendation.
- When suggesting a refactor, give the target boundary and the first safe step.
- Ask the human for judgment when the decision depends on taste, product direction, or future plans.
