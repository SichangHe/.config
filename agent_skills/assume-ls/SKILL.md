---
name: assume-ls
description: Use when documenting shared assumptions in ASSUM.md, referencing assumptions from code comments, or running assumls/assume-ls checks.
---

Document every *shared* assumptions in ASSUM.md of the deepest directory where
the assumption is used, write `# assumptions_name` followed by lines of
explanation to define it.
Reference them using `@ASSUME:asssumptions_name` in code comments.
`assumls check .` verifies.
