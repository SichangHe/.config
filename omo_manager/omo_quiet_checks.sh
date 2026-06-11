#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: omo_quiet_checks.sh [--tail N] [--timeout-s N] -- COMMAND [-- COMMAND ...]

Run test/check commands quietly and print only aggregate status, command names,
and failure details for executed commands. Failure tails are capped at 500 lines. Successful command output is suppressed; test pass counts are
intentionally omitted because passing tests are assumed to pass.
If the same repeated command set is used, put it in a dedicated tiny-output
wrapper script that calls this helper rather than re-pasting verbose commands.

Examples:
  omo_quiet_checks.sh -- "npm test" -- "npm run typecheck"
  omo_quiet_checks.sh --tail 80 -- "python -m unittest discover omo_manager/tests"
USAGE
}

tail_lines=120
max_tail_lines=500
timeout_s=300
commands=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --tail)
      if [ "$#" -lt 2 ]; then echo "--tail requires a number" >&2; exit 2; fi
      tail_lines="$2"
      shift 2
      ;;
    --timeout-s)
      if [ "$#" -lt 2 ]; then echo "--timeout-s requires a number" >&2; exit 2; fi
      timeout_s="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      if [ "$#" -lt 1 ]; then echo "-- requires a command string" >&2; exit 2; fi
      commands+=("$1")
      shift
      ;;
    *)
      commands+=("$1")
      shift
      ;;
  esac
done

case "$tail_lines" in
  ''|*[!0-9]*) echo "--tail must be a non-negative integer" >&2; exit 2 ;;
esac
case "$timeout_s" in
  ''|*[!0-9]*) echo "--timeout-s must be a positive integer" >&2; exit 2 ;;
esac
if [ "$tail_lines" -gt "$max_tail_lines" ]; then
  echo "--tail must be <= ${max_tail_lines}" >&2
  exit 2
fi
if [ "$timeout_s" -lt 1 ]; then
  echo "--timeout-s must be a positive integer" >&2
  exit 2
fi

if [ "${#commands[@]}" -eq 0 ]; then
  usage >&2
  exit 2
fi

tmpdir=$(mktemp -d /tmp/omo-quiet-checks.XXXXXX)
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

failed=0
failed_index=-1
failed_status=0
failed_log=""

for idx in "${!commands[@]}"; do
  log="$tmpdir/check-${idx}.log"
  if timeout --kill-after=5s "${timeout_s}s" bash -lc "${commands[$idx]}" >"$log" 2>&1; then
    continue
  else
    failed_status="$?"
  fi
  failed=1
  failed_index="$idx"
  failed_log="$log"
  break
done

if [ "$failed" -eq 0 ]; then
  echo "checks: pass"
  echo "commands:"
  for cmd in "${commands[@]}"; do
    printf -- '- %s\n' "$cmd"
  done
  exit 0
fi

echo "checks: fail"
echo "commands:"
for idx in "${!commands[@]}"; do
  if [ "$idx" -gt "$failed_index" ]; then
    break
  fi
  marker=""
  if [ "$idx" -eq "$failed_index" ]; then marker=" [failed exit=${failed_status}]"; fi
  printf -- '- %s%s\n' "${commands[$idx]}" "$marker"
done
echo "failure-output-tail:"
if [ "$tail_lines" -eq 0 ]; then
  echo "<suppressed>"
else
  tail -n "$tail_lines" "$failed_log" | sed 's/^/> /'
fi
exit "$failed_status"
