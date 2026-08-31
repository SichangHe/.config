#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
python_bin="$repo_root/omo_manager/.venv/bin/python"
ruff_bin="$repo_root/omo_manager/.venv/bin/ruff"

timeout 120s "$python_bin" -m unittest \
  omo_manager.tests.test_guest_hees_email_watcher \
  omo_manager.tests.test_guest_images \
  omo_manager.tests.test_guest_hees_pending_delivery \
  omo_manager.tests.test_completion_email \
  omo_manager.tests.test_manager_setup_watchers.WatcherSetupTests.test_setup_prepares_pinned_guest_hees_watcher
timeout 120s "$python_bin" "$repo_root/helper.sh/test_email_me.py"
timeout 60s "$ruff_bin" check \
  "$repo_root/helper.sh/email_me.py" \
  "$repo_root/helper.sh/test_email_me.py" \
  "$repo_root/omo_manager/email_idle_watcher.py" \
  "$repo_root/omo_manager/omo_completion_email.py" \
  "$repo_root/omo_manager/omo_email_config.py" \
  "$repo_root/omo_manager/omo_guest_images.py" \
  "$repo_root/omo_manager/omo_pending_watch.py" \
  "$repo_root/omo_manager/omo_email_subject.py" \
  "$repo_root/omo_manager/tests/test_guest_hees_email_watcher.py" \
  "$repo_root/omo_manager/tests/test_guest_hees_pending_delivery.py"
