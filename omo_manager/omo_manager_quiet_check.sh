#!/usr/bin/env bash
set -euo pipefail

cd "${OMO_MANAGER_CONFIG_ROOT:-$HOME/.config}"
env_root="${OMO_WORK_LOGS_ROOT+x}${OMO_WORK_LOGS_ROOT-}"
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
[ -n "$env_root" ] && OMO_WORK_LOGS_ROOT="${env_root#x}"
py_cmd="python3"
if command -v uv >/dev/null 2>&1; then
  py_cmd="uv run --project omo_manager python"
fi

omo_quiet_checks.sh \
  -- "bash -n omo_manager/omo_quiet_checks.sh omo_manager/omo_manager_quiet_check.sh omo_manager/omo_dispatch.sh omo_manager/omo_report.sh" \
  -- "$py_cmd -m unittest discover omo_manager/tests" \
  -- "$py_cmd - <<'PY'
import subprocess

result = subprocess.run(
    ['bash', 'omo_manager/omo_quiet_checks.sh', '--timeout-s', '1', '--tail', '20', '--', 'sleep 2'],
    capture_output=True,
    text=True,
    timeout=5,
    check=False,
)
if result.returncode != 124:
    raise SystemExit(f'expected timeout exit 124, got {result.returncode}: {result.stdout}{result.stderr}')
if '[failed exit=124]' not in result.stdout:
    raise SystemExit('missing timeout failure marker')
PY" \
  -- "$py_cmd - <<'PY'
from os import environ
from pathlib import Path
work_logs_root = Path(environ.get('OMO_WORK_LOGS_ROOT', str(Path.home() / 'work_logs')))
checks = {
    work_logs_root / 'MANAGER.md': ['Routine verification tests stay quiet', 'pass/fail aggregate only'],
    Path('omo_manager/docs/helpers/quiet-checks.md'): ['repeatedly called command set', 'tiny-output script'],
    Path('omo_manager/omo_dispatch.sh'): ['no test counts', 'repeated command set'],
    Path('omo_manager/omo_quiet_checks.sh'): ['Successful command output is suppressed', 'repeated command', '--timeout-s', '--kill-after'],
}
missing = []
for name, needles in checks.items():
    text = Path(name).read_text(encoding='utf-8')
    for needle in needles:
        if needle not in text:
            missing.append(f'{name}: {needle}')
if Path('MANAGER.md').exists():
    missing.append(f'MANAGER.md: remove config-root manager instructions; authoritative file is {work_logs_root / "MANAGER.md"}')
if missing:
    raise SystemExit('missing low-token wording: ' + '; '.join(missing))
PY"
