# guest hees policy

approved instruction source

- `/ssd1/sichangheagent/guest_hees/AGENTS.md`
- remains in effect until the primary human changes or revokes it

behavior

- the dedicated guest manager and its agents for `hees`
  - receive email from exactly `46496337@qq.com`
  - send email back to exactly `46496337@qq.com`
  - otherwise behave like any other agents
- guest research defaults
  - when a request arrives in another language
    - by default translate it into English before searching
    - translate the results back into that language
    - disclose that the search was conducted in English
  - use `pb-chatgpt-prompt-file` and applicable custom personal-browser search scripts
  - use `pb-gemini-snapshot` for a prepared public Gemini share
  - read `PB_BROWSER_SETUP_ROOT` from `~/.config/pb-browser-scripts.env`
  - consult these paths relative to that root
    - `docs/pb_search_engine_guidance.md`
    - `docs/script_driven_scanning.md`
    - `docs/google_gemini_browser_cli.md`

implementation

- policy approval does not activate production code
- `OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER` defaults to `false`
- enabling it starts a separate watcher with pinned sender, task, manager target, mail directory, and UID state
- guest subject tags never select another route
- authenticated guest image attachments are stored through the separately owned `omo_guest_images` interface before mail acceptance
- invalid image batches leave the guest message unread and unrouted
- guest replies pin the recipient when the manager or any agent uses its actual `guest_hees:*` producer target
- reply images require explicit `--guest-image-reference` values validated by `omo_guest_images.reply_attachments` before SMTP
