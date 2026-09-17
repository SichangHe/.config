---
description: "Read-only reviewer. Critique rewritten code without editing."
mode: subagent
model: openai/gpt-5.6-terra
variant: xhigh
temperature: 0.1
tools:
  write: false
  edit: false
  bash: false
  webfetch: true
permission:
  edit: deny
  bash: deny
  webfetch: allow
hidden: false
---

Read and follow
`https://raw.githubusercontent.com/SichangHe/sichanghe.github.io/refs/heads/main/src/notes/automation_software/agent_instructions/review.md`.
Do not edit files.
