#!/usr/bin/env bash
# Runs after every file edit: fast lint + type check.
# Exit 2 sends the errors back to Claude so it fixes them immediately.
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
out=""
fail=0

if [ -f package.json ]; then
  if [ -f node_modules/.bin/eslint ]; then
    r=$(npx --no-install eslint . --max-warnings=0 2>&1) || { fail=1; out+="ESLINT:\n$r\n"; }
  fi
  if [ -f tsconfig.json ] && [ -f node_modules/.bin/tsc ]; then
    r=$(npx --no-install tsc --noEmit 2>&1) || { fail=1; out+="TSC:\n$r\n"; }
  fi
elif [ -f pyproject.toml ] || [ -f setup.py ] || [ -f requirements.txt ]; then
  if command -v ruff >/dev/null; then
    r=$(ruff check . 2>&1) || { fail=1; out+="RUFF:\n$r\n"; }
  fi
  if command -v mypy >/dev/null; then
    r=$(mypy . 2>&1) || { fail=1; out+="MYPY:\n$r\n"; }
  fi
fi

if [ "$fail" -ne 0 ]; then
  printf "Lint/type errors after edit. Fix before continuing:\n%b" "$out" | tail -n 60 >&2
  exit 2
fi
exit 0
