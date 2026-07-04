# task file frontmatter

Generic task metadata live in YAML frontmatter. Helper scripts manager it.

ALL task files must have frontmatter, except for the main manager task files.

## source of truth

The frontmatter is the source of truth for task metadata.

(TODO: Remove below of the section after migration.)


Legacy prose markers are migration input only:

- `runat:` becomes `runat`
- `managerat:` becomes `managerat`
- `(running)`, `(blocked...)`, and `(done...)` become `status`
- blocked reason text becomes `blocked_on`
- bullets above `(above are pending task items)` become `pending_task_items`

## draft schema

```yaml
---
version: v1.0.0
status: blocked
blocked_on: blocked reason
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
pending_task_items:
  - goal 1
---
```

## fields

Avoid unnecessary fields.

Reject fields that are not in the schema. All comments stay inside Markdown bodys in `()` instead.

`status` can and can only be one of:

- `running`
- `blocked`
- `done`

If `status: blocked`, `blocked_on` is required; else, `blocked_on` MUST NOT exist.
ALL other fields are required.

`managerat` MUST be different from `runat`. If `is_manager` is `true`, send pending messages to the tmux window at `managerat`; otherwise, send to `runat`.

`pending_task_items` only contains items that are still open. Remove done/cancelled items IMMEDIATELY.

## helper behavior

`omo_task.py` creates new task files with correct placeholder frontmatter. `status` is `running`; `managerat` is the current tmux window; `runat`, `tool`, `is_manager` are mandatory arguments passed in; `pending_task_items` is empty. After a successful return, this script reminds the caller to fill in `pending_task_items`.

`omo_agent_status.py` only reads from frontmatter.

`omo_pending_watch.py` scans for `(pending)` markers and dispatches lines from there to the end of the task file according to `is_manager`, `runat`, `managerat`, etc. It then remembers the dispatch for 10min before possibly re-dispatching. The target manager is responsible for removing the `(pending)` marker.

`email_idle_watcher.py` and `omo_report.sh` simply append `(pending)` and the message body to the task file.

## migration plan

Migrate active task files only.

For each active task file:

1. Parse existing **bottom** `runat:` and `managerat:`.
2. Parse the latest non-pending status marker.
3. If latest status is blocked, move the reason into `blocked_on`.
4. Move bullets above `(above are pending task items)` into `task_items`.
5. Add frontmatter.
6. Leave the body as is.

- [ ] need to change MANAGER.md to match these new behavior
