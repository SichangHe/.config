---
name: fragile-git-ops
description: Safe noninteractive workflow for fragile Git operations such as interactive rebase, conflict resolution, commit-message editing, selective staging, and push recovery. Use when Git may open an editor, rewrite history, merge divergent work, or require careful preservation of both sides of a conflict.
---

# Fragile Git Ops

Run Git operations so the agent never gets trapped in an interactive editor and never loses unrelated work.

## Interactive Editors

- Use noninteractive editor scripts for Git commands that may open an editor.
- Preview before editing:
  - run a script that copies the editor file to a private temp path
  - empty the editor file to abort the first attempt
  - inspect the preview
- Proceed only when the preview has expected content.
- For the real run, use a script that writes the exact intended content.
- Reuse existing commit messages or todo files unchanged when they are non-empty and acceptable.

## Conflicts

- Understand both sides before editing.
- Preserve both behaviors when they are compatible.
- Prefer the smallest conflict resolution that restores the intended combined behavior.
- Run focused checks after conflict resolution.

## Staging And Commit

- Stage only files belonging to the current task.
- Keep commits atomic and use conventional messages when the user asks for a commit.
- Pull with rebase if push fails because the remote advanced.
- Never commit secrets, absolute local paths, or unrelated generated churn.
