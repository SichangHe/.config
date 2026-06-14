---
name: fragile-git-ops
description: "Use for safe Git interactive rebase, conflict resolution, commit-message editing, history rewrite, and other Git operations that may open an editor or require preserving both sides of a change."
---

Instructions for interactive rebase:
ALWAYS pass in non-interactive script as `EDITOR` for all Git commands to avoid stuck
E.g. when editing Git-TODO or commit message
First, to see what the editor would be editing, use script that print out file content elsewhere AND THEN EMPTY THE FILE to abort Git
Then, to actually edit, use a script that directly writes desired content to file
If the preview file contains a non‑empty commit message or todo, proceed by writing it back unchanged with the write script. Only ask user if the preview content is empty or I explicitly asked to edit it. Existing commit messages are acceptable by default
For each conflict, figure out yourself what each side is doing, and combine the changes in a way that preserves both functionalities
