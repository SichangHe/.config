# 🤖 agent skills setup

🤖 Human-review note: review and approve these shared skill instructions and integration paths.

🤖 canonical source
- 🤖 `/home/sichangheagent/.config/agent_skills/*/SKILL.md`
- 🤖 one canonical copy per skill
- 🤖 tool-specific locations link back here

🤖 Codex discovery
- 🤖 live user skills
  - 🤖 `/home/sichanghe/.codex/skills/*`
- 🤖 tracked Codex template
  - 🤖 `/home/sichangheagent/.config/conf_template/codex/skills/*`

🤖 OpenCode discovery
- 🤖 native skill path
  - 🤖 `/home/sichangheagent/.config/opencode/skills/*`
- 🤖 OpenCode 1.15.12 exposes a local skill API schema for `/skill`
- 🤖 runtime verification blocker
  - 🤖 `opencode agent list` hung
  - 🤖 a temporary `opencode serve --pure` `/skill` probe timed out
  - 🤖 filesystem installation is complete, but live OpenCode enumeration was not proven in this environment

🤖 Claude Code and generic agent discovery
- 🤖 Claude Code compatibility path
  - 🤖 `/home/sichangheagent/.claude/skills/*`
- 🤖 generic agent convention path
  - 🤖 `/home/sichangheagent/.agents/skills/*`

🤖 Plot Code blocker
- 🤖 no `plotcode` or `plot-code` binary was found on `PATH`
- 🤖 no local Plot Code config or skill-loader path was found under `/home/sichanghe`, `/home/sichangheagent`, or `/ssd1/sichangheagent`
- 🤖 if `Plot Code` means `Claude Code`, the skills are installed through `/home/sichangheagent/.claude/skills/*`
- 🤖 otherwise the missing blocker is the unknown Plot Code executable/config contract
