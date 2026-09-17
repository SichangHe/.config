# agent tree

(authored by agents unless marked 🧑)

purpose

- print authoritative manager-to-report relationships as a top-down tree
- show each task's declared role, lifecycle status, recorded assignment purpose, and current work
- reject state that would make complete accounting false or ambiguous

source of truth

- task frontmatter supplies `runat`, `managerat`, `is_manager`, status, queue,
  and blocker
- the first manager-delegation prose paragraph supplies purpose; a task without
  one uses its first Human-instruction prose paragraph
- the exact historical `202608/close_agents_1256.md` and
  `202608/unslop_skill_repair_1119.md` bodies have digest-bound display-only
  purpose fallbacks; other paths or body prose remain untrusted
- the configured main-manager target supplies the full-tree root
- pane text never creates an agent, role, or reporting edge

interface

- `omo_agent_tree.py --help` is the sole command reference
- other documentation names the helper and points to its help without copying
  flags, defaults, or examples
