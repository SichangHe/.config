#!/usr/bin/env bash
set -euo pipefail

cd "${OMO_MANAGER_CONFIG_ROOT:-$HOME/.config}"
py_cmd="python3"
if command -v uv >/dev/null 2>&1; then
  py_cmd="uv run --project omo_manager python"
fi

omo_quiet_checks.sh \
  -- "bash -n omo_manager/omo_quiet_checks.sh omo_manager/omo_manager_quiet_check.sh omo_manager/omo_dispatch.sh omo_manager/omo_report.sh" \
  -- "$py_cmd -m unittest discover omo_manager/tests" \
  -- "$py_cmd - <<'PY'
from pathlib import Path
checks = {
    'MANAGER.md': ['Do not report how many tests passed', 'repeatedly called commands'],
    'omo_manager/MANAGER_HELPERS.md': ['repeatedly called command set', 'tiny-output script'],
    'omo_manager/omo_dispatch.sh': ['no test counts', 'repeated command set'],
    'omo_manager/omo_quiet_checks.sh': ['Successful command output is suppressed', 'repeated command'],
}
missing = []
for name, needles in checks.items():
    text = Path(name).read_text(encoding='utf-8')
    for needle in needles:
        if needle not in text:
            missing.append(f'{name}: {needle}')
if missing:
    raise SystemExit('missing low-token wording: ' + '; '.join(missing))
PY"
