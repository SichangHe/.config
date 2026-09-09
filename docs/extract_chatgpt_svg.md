extract_chatgpt_svg

- purpose
  - extract one fenced SVG document from a captured ChatGPT Markdown response
  - reject active content and non-local asset references
- interface
  - first argument: captured Markdown input
  - second argument: SVG output
  - executable is installed directly in `bin`
- control flow
  - find exactly one complete `svg` fence
  - parse its XML
  - validate every element and attribute
  - create the output parent and write the SVG
- failure
  - invalid or unsafe input raises an error before output is written
- provenance
  - moved from `agent_skills/svg-diagram-plotting/scripts/extract_svg.py`
  - behavior is unchanged by the move
