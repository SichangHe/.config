# pending watcher

- purpose
  - deliver new Markdown `(pending)` markers to the manager with file-line refs
  - keep seen-state durable so watcher restarts do not redeliver the same marker
  - run agent-problem and digest maintenance without delaying marker scans

- file-change path
  - Linux uses recursive inotify watches on the work-log root
  - `.git`, `.venv`, and `__pycache__` dirs are ignored
  - Markdown file events enqueue only changed files for marker parsing
  - directory create, move, watch removal, unmount, or queue overflow forces a full Markdown scan
  - platforms without inotify use the older mtime scan fallback

- safety scans
  - startup scans all Markdown files
  - periodic full scans remain controlled by `--full-scan-interval-s`
  - full scans refresh the fallback mtime snapshot

- subprocess isolation
  - pending dispatch uses the existing `omo_push_to_manager.py` path
  - agent-problem checks run as background child processes and are polled
  - digest delivery runs as a background child process and is polled
  - timeout handling kills overdue maintenance children and logs stderr

- assumptions
  - production manager hosts are Linux and support inotify
  - a periodic full scan is acceptable as a recovery mechanism
  - maintenance command output remains small enough for captured pipes
