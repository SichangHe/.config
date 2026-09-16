getagentsmd

- source
  - root: notes `automation_software/AGENTS.md`
  - named document: notes `automation_software/agent_instructions/NAME.md`
- commands
  - no arguments prints the root
  - `get NAME` prints only the explicitly named document
- launcher use
  - fresh prompted launches prepend the exact root command and its stdout
  - retrieval failure stops launch before task, TODO, session, or pane mutation
- retrieval
  - try GitHub first
  - cache each successful response separately
  - use that document's private local cache after a remote failure
- assumptions
  - instruction names use lowercase letters, digits, or `_`
  - the root and named documents supply the reference tree
  - the executable does not infer a language or follow child choices
