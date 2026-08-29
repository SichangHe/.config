# guest image producer interface

status

- image-specific contract is ready
- email watcher and reply-sender wiring remains inactive until the guarded scaffold owner applies and reviews it
- browser upload mechanics remain owned by PB

runtime root

- authoritative root: `${OMO_MANAGER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager}/guest-images`
  - current default: `/home/sichangheagent/.local/state/omo-manager/guest-images`
  - `OMO_GUEST_IMAGE_ROOT` is a test/development override, not a production routing mechanism
- reference root: `guest-image:v1:`
- no root, object, receipt, or quarantine may be inside a Git repository or traverse a symlink
- root and directories are owner-only `0700`; files are owner-only `0600`

inbound boundary

- call `omo_guest_images.store_message_images` only after the watcher authenticates the exact sender
- required arguments
  - `sender="46496337@qq.com"`
  - `route_target="guest_hees:0"`
  - `authentication="exact-visible-sender-and-gmail-transport-spf/v1"`
  - stable mailbox-scoped `source_id`
  - parsed MIME `message`
- result
  - ordered tuple of `guest-image:v1:<lowercase sha256>` references
  - empty tuple when the message has no attachments
  - any invalid attachment rejects the entire image batch without a reference
- accepted declared and sniffed MIME/extension combinations
  - `image/png` and `.png`
  - `image/jpeg` and `.jpg` or `.jpeg`
  - `image/gif` and `.gif`
  - `image/webp` and `.webp`
- each image part requires exactly one base64 content-transfer encoding
  - encoded length is bounded before decoding
- bounds
  - 4 images per message
  - repeated identical image attachments are rejected
  - 10 MiB per image
  - 20 MiB image total per message
  - 100 stored image objects
  - 100 MiB total stored object and receipt bytes
  - 1,000 stored batches

batch receipt

- path: `batches/<sha256(source_id)>.json`
- serialization

```json
{
  "schema": "omo-guest-image-batch/v1",
  "state": "active",
  "source_id": "<mailbox-scoped source id>",
  "received_at": "<UTC ISO 8601 timestamp>",
  "sender": "46496337@qq.com",
  "route_target": "guest_hees:0",
  "authentication": "exact-visible-sender-and-gmail-transport-spf/v1",
  "images": [
    {
      "filename": "<original basename>",
      "mime_type": "image/png",
      "size_bytes": 123,
      "sha256": "<lowercase sha256>",
      "reference": "guest-image:v1:<lowercase sha256>",
      "object": "objects/<lowercase sha256>.png"
    }
  ]
}
```

PB resolution boundary

- command
  - `python -m omo_manager.omo_guest_images resolve --service SERVICE --reference REFERENCE`
- supported `service` values
  - `chatgpt`
  - `gemini`
- successful stdout is one JSON object

```json
{
  "schema": "omo-guest-image-resolution/v1",
  "service": "chatgpt",
  "reference": "guest-image:v1:<lowercase sha256>",
  "path": "<absolute active object path>",
  "sha256": "<lowercase sha256>",
  "mime_type": "image/png",
  "size_bytes": 123,
  "batch_receipt": "<absolute active batch receipt path>",
  "batch_receipt_sha256": "<lowercase sha256>",
  "sender": "46496337@qq.com",
  "route_target": "guest_hees:0",
  "authentication": "exact-visible-sender-and-gmail-transport-spf/v1"
}
```

- PB verifies before each upload
  - command exits `0`
  - `schema`, `service`, `reference`, `sender`, `route_target`, and `authentication` are exact
  - `sha256` equals the digest suffix in `reference`
  - `batch_receipt_sha256` is present
- PB may rely on the successful resolver call for the active path, regular-file/no-symlink check, owner-only permissions, digest, MIME sniff, extension, size, count, exact sender, and route
- PB reruns resolution immediately before each upload and does not reuse cached paths or resolution JSON
- PB does not copy the image into a repository

research route pointers

- read `PB_BROWSER_SETUP_ROOT` from `~/.config/pb-browser-scripts.env`
- route selection: `$PB_BROWSER_SETUP_ROOT/docs/pb_search_engine_guidance.md`
- command usage: `$PB_BROWSER_SETUP_ROOT/docs/script_driven_scanning.md`
- Gemini boundaries: `$PB_BROWSER_SETUP_ROOT/docs/google_gemini_browser_cli.md`
- ChatGPT default research helper: `pb-chatgpt-prompt-file`
- Gemini default research helper: `pb-gemini-snapshot`
  - current custom personal-browser helper, not a general Gemini prompt CLI

outbound reply boundary

- call `omo_guest_images.reply_attachments` with explicitly selected references
- required `recipient`: exactly `46496337@qq.com`
- sender and recipient checks use literal equality, not case folding or mailbox canonicalization
- result: validated immutable bytes and MIME for 1 to 4 unique active references
- the guarded sender owner attaches those bytes only after this call succeeds and only to the already pinned guest recipient

retention and failure

- no scheduled cleanup and no permanent deletion
- stores durably publish state `planned` before objects and atomically change it to `active` only after every object is durable
  - retrying the same `source_id` and exact image list completes an interrupted planned batch
- quarantine receipts move from `planned` to `quarantined`; restore receipts move from `restoring` to `restored`
  - every move fsyncs its source and destination directory
  - the next store, resolve, cleanup, or restore completes an interrupted quarantine transition from exact receipt evidence before serving data
- explicit `cleanup --older-than-days N` moves expired batches and unshared objects into a bounded owner-private quarantine
- each quarantine has `receipt.json` with schema `omo-guest-image-quarantine/v1`, exact source/destination, size and SHA-256 evidence for every move, age, timestamp, and state
- quarantined references stop resolving and therefore fail closed for PB and email replies
- explicit `restore --receipt RECEIPT` restores one exact quarantine and changes its receipt state to `restored`
  - if an identical object became active while quarantined, restore preserves the quarantined duplicate and records its path in `retained_quarantined_duplicates`
- path, symlink, permission, MIME, extension, size, count, digest, receipt, sender, recipient, route, service, or capacity mismatch returns a nonzero result and produces no usable reference
