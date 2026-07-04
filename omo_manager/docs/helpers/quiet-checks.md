# quiet check helpers

`omo_quiet_checks.sh` is the low-token aggregate test/check runner. Agents should run required verification and ad hoc manager-visible diagnostics as `omo_quiet_checks.sh -- "COMMAND" [-- "COMMAND" ...]` when practical.

On success it prints only `checks: pass` and the command names. On failure it prints `checks: fail`, the executed command list with the failed exit status, and a bounded failure-output tail capped by the helper. Manager-facing reports must not include counts of passed tests or verbose successful test logs; include only aggregate pass/fail, command names, and failures/blockers.

For any repeatedly called command set, add a dedicated tiny-output script wrapper, for example `omo_manager_quiet_check.sh` or `*_quiet_check.py`, instead of asking agents to paste the full command list repeatedly. The wrapper may call `omo_quiet_checks.sh` internally or implement the same contract directly: successful output is suppressed, failures include only the failed command/check name and bounded failure details capped by the helper.

`omo_manager_quiet_check.sh` is the aggregate validation entrypoint for this manager-helper workflow.
