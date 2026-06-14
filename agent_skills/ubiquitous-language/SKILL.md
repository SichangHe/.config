---
name: ubiquitous-language
description: Codebase terminology extraction and normalization for creating a shared developer-and-agent language. Use when asked to scan a codebase for domain terms, produce terminology tables, align naming across docs/code/tests, or make a glossary for future implementation and review.
---

# Ubiquitous Language

Build a compact glossary from the codebase, not from guesses.

## Workflow

- Inspect existing names before proposing terminology:
  - public types, modules, functions, routes, commands, migrations, schemas, configs, tests, docs, and error text
  - repeated synonyms and near-synonyms
  - terms that encode business rules, protocol states, roles, units, or lifecycle phases
- Record only terms that help future code, docs, prompts, tests, or reviews.
- Prefer existing dominant names unless they are misleading.
- Mark conflicts instead of silently picking a winner.
- Preserve project casing and spelling in examples.
- Put the output where the repo already keeps architecture or glossary notes; otherwise create a small Markdown file such as `docs/ubiquitous-language.md`.

## Table Shape

Use Markdown tables with these columns:

- term
- meaning
- source examples
- preferred use
- conflicts or aliases
- confidence

## Quality Bar

- Every term must cite concrete file or symbol evidence.
- Every proposed rename must explain the ambiguity it removes.
- Every unresolved conflict must name the human decision needed.
