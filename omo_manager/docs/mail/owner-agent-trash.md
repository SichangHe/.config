# owner agent-mail Trash

- command
  - `uv run --offline --python 3.13 python ~/.config/bin/omo_manager_mail_compress.py owner-trash-agent-mail`
- scope
  - every message from the distinct configured agent sender in the Human Inbox
  - includes Bcc, Cc, and multiple-recipient delivery
  - Human-sent mail never matches
- operation
  - reads headers with `BODY.PEEK` and reads Gmail metadata
  - performs one recoverable Gmail `MOVE` when matches exist
  - never writes read flags, permanently deletes, or expunges
- receipt
  - one JSON object on success
  - exact source UID, Gmail message identity, resulting Trash UID, and original unread state per moved message
  - observed Inbox total and unread counts immediately before and after the move
