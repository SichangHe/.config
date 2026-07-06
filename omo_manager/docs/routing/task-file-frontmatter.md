# task file frontmatter

Generic task metadata live in YAML frontmatter. Helper scripts manage it.

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

Reject fields that are not in the schema. All comments stay inside Markdown bodies in `()` instead.

`status` can and can only be one of:

- `running`
- `blocked`
- `done`

If `status: blocked`, `blocked_on` is required; else, `blocked_on` MUST NOT exist.
ALL other fields are required.

`managerat` MUST be different from `runat`. In a worker task file, `managerat` is the manager that owns the task. In a manager task file, `runat` is the manager pane that receives pending blocks already written to that manager file. Use worker `runat` only for the worker pane and direct-message delivery.

`pending_task_items` only contains items that are still open. Remove done/cancelled items IMMEDIATELY.

## helper behavior

`omo_task.py` creates new task files with correct placeholder frontmatter. `status` is `running`; `managerat` is the current tmux window; `runat`, `tool`, `is_manager` are mandatory arguments passed in; `pending_task_items` is empty. After a successful return, this script reminds the caller to fill in `pending_task_items`.

`omo_agent_status.py` only reads from frontmatter.

`omo_pending_watch.py` scans for `(pending)` markers and dispatches lines from there to the end of the task file. Worker task files route normal pending blocks to `managerat`; manager task files route normal pending blocks to their own `runat`. It then remembers the dispatch in process memory before possibly re-dispatching. The target manager is responsible for recording `pending_task_items`, removing the `(pending)` marker, and routing the work. Worker `runat` is used for direct-message worker delivery only.

`omo_report.sh` reads the reporting worker task file, finds its `managerat`, and appends the `(pending)` report block to that manager's task file. If `managerat` is the main manager target, the destination is the dated `work_manager_YYYY-MM-DD.md` file.

`email_idle_watcher.py` appends human email pending blocks to the current main manager file or to the addressed manager task file. When an addressed task is a worker task, its `managerat` is used to find the current manager file that should receive the block.

## migration plan

Migrate active task files only.

For each active task file:

1. Parse existing **bottom** `runat:` and `managerat:`.
2. Parse the latest non-pending status marker.
3. If latest status is blocked, move the reason into `blocked_on`.
4. Move bullets above `(above are pending task items)` into `pending_task_items`.
5. Add frontmatter.
6. Leave the body as is.

- [ ] finish migrating active task files
