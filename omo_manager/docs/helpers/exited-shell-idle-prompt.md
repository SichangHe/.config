# exited shell idle prompt

- purpose
  - distinguish Codex exit UI residue from shell activity before guarded pane closure
- accepted tail
  - one nonempty legacy tail line under the existing close contract
  - exact styled `⏎` Codex exit marker followed by one styled Fish prompt with no input before the host-and-working-directory right prompt and with its cursor at the empty-input column
- rejected tail
  - any additional line before or after the prompt
  - changed pane, process, session, capture, task, TODO, or report evidence
- scope
  - parsing only
  - no input, resume, task edit, or pane close during preparation
