# codex compact when idle

`omo_codex_compact_when_idle.py --target TARGET` is the dedicated Codex `/compact` path. It polls `omo_codex_status.py` until the target reports `ready`, then sends `/compact` through `omo_tmux_send.py`'s verified buffer-paste path.

It never sends `/compact` while status is anything other than `ready`; on timeout it reports the last status. Use `--background` to detach the wait/send worker, `--notify-target CALLER` only when `CALLER` is a different pane from `TARGET`, `--timeout-s N` to cap waiting, and `--log-file PATH` to control the background log location.

Self-compaction should omit `--notify-target` and rely on the printed/logged worker result:

```sh
omo_codex_compact_when_idle.py --target cfg:1.0 --background --notify-target cfg:0.0 --timeout-s 1800
omo_codex_compact_when_idle.py --target cfg:1.0 --background --timeout-s 1800
```
