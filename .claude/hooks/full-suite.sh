#!/usr/bin/env bash
# Runs when Claude tries to finish: full test suite must be green.
# Exit 2 blocks "done" and sends the failures back to Claude.
input=$(cat)
# Avoid an endless loop: if Claude is already continuing because of this hook, let it stop.
if printf '%s' "$input" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

if [ -f package.json ] && grep -q '"test"' package.json; then
  out=$(CI=true npm test 2>&1); code=$?
elif [ -f pyproject.toml ] || [ -f pytest.ini ] || [ -d tests ]; then
  command -v pytest >/dev/null || exit 0
  out=$(pytest -q 2>&1); code=$?
  [ "$code" -eq 5 ] && exit 0   # pytest: no tests collected
else
  exit 0
fi

if [ "$code" -ne 0 ]; then
  printf "Full test suite is failing. Not done yet. Fix these:\n%s\n" "$(printf '%s' "$out" | tail -n 80)" >&2
  exit 2
fi
exit 0
