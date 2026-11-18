#!/usr/bin/env bash
# One command, every quality gate: lint -> types -> tests + 100% branch coverage -> mutation score.
# Usage:  bash precise-check.sh            (all gates)
#         SKIP_MUTATION=1 bash precise-check.sh   (fast run, no mutation testing)
# Tunables: COVERAGE_MIN (default 100), MUTATION_MIN (default 90), SRC (default src)
set -u
COVERAGE_MIN="${COVERAGE_MIN:-100}"
MUTATION_MIN="${MUTATION_MIN:-90}"
SRC="${SRC:-src}"
fail=0
step() { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }
gate() { if [ "$1" -eq 0 ]; then echo "PASS: $2"; else echo "FAIL: $2"; fail=1; fi; }

if [ -f package.json ]; then
  step "Lint (eslint)";        npx --no-install eslint . --max-warnings=0; gate $? "lint"
  if [ -f tsconfig.json ]; then
    step "Types (tsc)";        npx --no-install tsc --noEmit; gate $? "type check"
  fi
  step "Tests + branch coverage >= ${COVERAGE_MIN}% (vitest)"
  npx --no-install vitest run --coverage --coverage.thresholds.branches="$COVERAGE_MIN"; gate $? "tests + coverage"
  if [ -z "${SKIP_MUTATION:-}" ]; then
    step "Mutation testing (stryker, break threshold set in stryker.config.json)"
    npx --no-install stryker run; gate $? "mutation score"
  fi

elif [ -f pyproject.toml ] || [ -f setup.py ]; then
  step "Lint (ruff)";          ruff check . --extend-exclude mutants; gate $? "lint"
  step "Types (mypy)";         mypy "$SRC"; gate $? "type check"
  step "Tests + branch coverage >= ${COVERAGE_MIN}% (pytest)"
  pytest -q --ignore=mutants --cov="$SRC" --cov-branch --cov-report=term-missing --cov-fail-under="$COVERAGE_MIN"; gate $? "tests + coverage"
  if [ -z "${SKIP_MUTATION:-}" ]; then
    step "Mutation testing (mutmut)"
    mutmut run >/dev/null 2>&1
    res=$(mutmut results --all true 2>/dev/null)
    total=$(printf '%s\n' "$res" | grep -cE ': (killed|survived|timeout|suspicious|no tests)$')
    killed=$(printf '%s\n' "$res" | grep -cE ': (killed|timeout)$')
    if [ "$total" -gt 0 ]; then
      score=$(( killed * 100 / total ))
      echo "Mutation score: ${score}% (${killed}/${total} killed). Target: ${MUTATION_MIN}%"
      printf '%s\n' "$res" | grep -E ': (survived|no tests)$' | sed -E 's/^[[:space:]]*/  surviving -> mutmut show /; s/: [a-z ]+$//'
      [ "$score" -ge "$MUTATION_MIN" ]; gate $? "mutation score"
    else
      echo "No mutants generated (check [tool.mutmut] source_paths)"; gate 1 "mutation score"
    fi
  fi
else
  echo "No package.json or pyproject.toml found."; exit 1
fi

step "RESULT"
if [ "$fail" -eq 0 ]; then echo "ALL GATES PASSED"; else echo "GATES FAILED - not done"; fi
exit "$fail"
