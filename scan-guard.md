# scan-guard

(authored by agents unless marked 🧑)

🧑 "wrappers of all the disk scan commands ... put them on path ... stop by
default after some amount of scanning and prompt the user to take measures to
avoid huge disk scanning, and have a `--dangerously-blah` flag to override"

why
- huge agent disk scans were part of the load when `/hdd1` stalled
    - 2026-10-03 and 2026-10-11; see `work_logs/hdd1_fault_2026-10-11_summary.md`

what
- one script, `bin/scan-guard`; it acts as the command whose name it is run under
    - install: symlink `find`, `grep`, `rg`, `fd`, `du`, `tree` to it in a directory ahead of the real ones on `PATH`
        - installed 2026-10-12 in `~/.config/bin`; undo by removing those six symlinks
        - `~/.config/bin` comes first in fish (`fish/path.fish`) and bash login shells (`~/.profile`)
        - zsh and the Omnigent service do not put it first yet, so the same six symlinks also sit in `~/.local/bin` until they do
    - real command = first same-named executable on `PATH` that is not this script
- amount of scanning = seconds during which the command read from disk
    - sampled once per second from `read_bytes` in `/proc/PID/io`
    - limit: `SCAN_GUARD_MAX_DISK_READ_S`, default 30
    - seconds w/o disk reads are free
        - so a filter on a pipe (`tail -f log | grep x`) is never stopped
        - so a search of cached files is never stopped
- at the limit: kill the command, print how to narrow the scan, exit 124
- override: `--dangerously-scan-without-limit` anywhere in the arguments
    - removed before the real command runs

control flow
- override given → `exec` the real command
- else start the real command and a watcher, both in the background
    - watcher loops forever unless the limit is hit; then it kills the command and returns
    - main waits for the command, kills the watcher, reads the watcher's exit status
        - 0 → the watcher stopped the command → exit 124
        - else → exit w/ the command's status
- `TERM`/`INT`/`HUP` to the wrapper are passed to the command

assumptions and gaps
- only the command's own process is counted, not processes it starts (`find -exec`)
- reads from network storage (`/synology`) do not show in `read_bytes`, so they are not limited
- not covered: a command run by absolute path (`/usr/bin/find`)
- not covered: Claude Code's Bash tool
    - its shell defines `find` and `grep` as functions that run the `claude` binary as `bfs` and `ugrep`
    - functions win over `PATH`; both scans running on 2026-10-11 were of this kind
    - the `claude` setting `EMBEDDED_SEARCH_TOOLS=0` does not remove them (tested)
    - covered by a rule in `~/.claude/CLAUDE.md` instead
        - 🧑 "Never ever call built-in functions to scan disks; use commands."
        - `command find` and `command grep` reach the wrappers
- not covered: `rg` and `fd` wherever `~/.nix-profile/bin` comes before both symlink directories

## Claude Code embedded grep/find (Oct 23)

Claude Code's shell snapshot defines `grep` and `find` as functions running its
embedded ugrep and bfs, which bypassed these wrappers (one grep ran 6 h 50 min
over /hdd1 on Oct 22). The PreToolUse hook `bin/claude-unshadow-scans`, set in
`~/.claude/settings.json` for Bash, prefixes each command with
`unset -f grep find rg`, so the wrappers on PATH win. Setting
`EMBEDDED_SEARCH_TOOLS=0` or `false` did not turn the functions off.
