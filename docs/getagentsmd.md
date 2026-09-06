getagentsmd

- source
  - root: notes `automation_software/AGENTS.md`
  - catalog: notes `automation_software/agent_instructions/index.md`
  - named document: notes `automation_software/agent_instructions/NAME.md`
- commands
  - no arguments prints the root
  - `list` prints the catalog
  - `get NAME` prints only the explicitly named document
- retrieval
  - try GitHub first
  - cache each successful response separately
  - use that document's private local cache after a remote failure
- assumptions
  - instruction names use lowercase letters, digits, `_`, or `-`
  - document text supplies hierarchy and optional child choices
  - the executable does not infer a language or follow child choices
