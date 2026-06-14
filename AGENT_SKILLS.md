# agent skills setup

Human-review note: review and approve these shared skill instructions and integration paths.

canonical source
- `/home/sichangheagent/.config/agent_skills/*/SKILL.md`
- one canonical copy per skill
- tool-specific locations link back here

Codex discovery
- live user skills
  - `/home/sichanghe/.codex/skills/*`
- tracked Codex template
  - `/home/sichangheagent/.config/conf_template/codex/skills/*`
- Codex loads skill names, descriptions, and paths into the model-visible skills instructions block
  - verified in this session
  - source reference: `/ssd1/sichangheagent/openai--codex/codex-rs/core-skills/src/render.rs`

OpenCode discovery
- native skill path
  - `/home/sichangheagent/.config/opencode/skills/*`
- OpenCode-style harnesses expose skill names and descriptions through the `skill` tool
  - source reference: `/ssd1/sichangheagent/youngbinkim0--oh-my-opencode/src/tools/skill/tools.ts`
- OpenCode-style task delegation injects selected skill bodies through `load_skills`
  - source reference: `/ssd1/sichangheagent/youngbinkim0--oh-my-opencode/src/tools/delegate-task/tools.ts`
  - source reference: `/ssd1/sichangheagent/youngbinkim0--oh-my-opencode/src/tools/delegate-task/skill-resolver.ts`
  - source reference: `/ssd1/sichangheagent/youngbinkim0--oh-my-opencode/src/tools/delegate-task/prompt-builder.ts`
- runtime verification blocker
  - prior `opencode agent list` hung
  - prior temporary `opencode serve --pure` `/skill` probe timed out
  - filesystem installation is complete, but live OpenCode enumeration was not proven in this environment

Claude Code and generic agent discovery
- Claude Code compatibility path
  - `/home/sichangheagent/.claude/skills/*`
- generic agent convention path
  - `/home/sichangheagent/.agents/skills/*`
- skill files are installed by symlink, but live description loading was not verified for these harnesses

Plot Code blocker
- no `plotcode` or `plot-code` binary was found on `PATH`
- no local Plot Code config or skill-loader path was found under `/home/sichanghe`, `/home/sichangheagent`, or `/ssd1/sichangheagent`
- if `Plot Code` means `Claude Code`, the skills are installed through `/home/sichangheagent/.claude/skills/*`
- otherwise the missing blocker is the unknown Plot Code executable/config contract
