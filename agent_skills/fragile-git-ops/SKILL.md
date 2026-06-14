---
name: fragile-git-ops
description: "Use for safe Git interactive rebase, conflict resolution, commit-message editing, history rewrite, and other Git operations that may open an editor or require preserving both sides of a change."
---

Supply noninteractive scripts as `EDITOR` for Git commands that
may open an editor.
Before editing, use a script that: copies the editor file to a temp path,
empties the editor file to abort the attempt; then read that temp path.
For the real run, use a script that writes the exact intended content.
Reuse existing commit messages unchanged when acceptable.

Conflicts: Understand both sides before editing.
Preserve both behaviors when they are compatible.
Prefer the smallest conflict resolution that
restores the intended combined behavior.
Run focused checks after conflict resolution.
