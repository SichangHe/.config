# AMH Human-email route launch

`omo_amh_route_launch.py` launches one configured worker for a ready AMH Human-email route after the watcher has committed AMH ingress. It is not a production cutover path.

The launcher asks AMH for `task human-route-status`, requires `route_kind=human_email`, `state=ready`, Gmail provider metadata, an exact subject whose one leading tag matches the destination agent (`[main]` maps to `main-manager`), and a matching payload digest. It then writes a 0600 prompt, writes `lifecycle-binding.json`, and calls `omo_task.py` with `--tool codex`, `--amh-caller-agent`, `--require-existing-tmux-session`, and no `--is-manager`.

`lifecycle-binding.json` is the local AMH-owned runner evidence. It binds `destination_agent_id`, `amh_runner_agent`, and `amh_caller=agent:{amh_runner_agent}` to the route operation, source, request, prompt digest, and optional AMH `agent_spec`. A completed `launch-receipt.json` is valid only when it repeats the same runner binding and stores the binding file digest.

The configured tmux session and workdir must already exist. The launcher does not create `/ssd1/sichangheagent/amh` or any other workdir. Missing session or workdir fails closed before `omo_task.py`. Session names starting with `h` are rejected.

Launch state lives under `{state-dir}/amh-route-launches/{route-id}/`. A valid `launch-receipt.json` is idempotent. A `launch.lock` blocks automatic replay after an uncertain `omo_task.py` failure so recovery stays manual. Prelaunch failures and `--dry-run` clear the lock.

Direct Human replies use `email_me.py --subject-file --message-file` with subject exactly `Re: {exact_subject}`. Workers do not ask the current manager to proxy Human mail.
