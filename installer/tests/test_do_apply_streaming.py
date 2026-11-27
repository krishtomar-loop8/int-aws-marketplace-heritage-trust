"""Regression test for do_apply()'s tee-streamed apply log (installer/deploy.sh).

This mirrors do_apply()'s actual control flow (including the real on_error/fail/trap
wiring from deploy.sh) under real bash, so a regression like streaming the apply log
through `tee` without preserving `tf apply`'s own exit code -- or accidentally tripping
the ERR trap on the pipeline itself -- fails this test the same way the original
`set +e`/`set -e` mistake would have.
"""

import os
import shutil
import subprocess

# See test_classify_apply_failure.py for why bash is resolved via shutil.which rather
# than invoked bare (Windows can otherwise resolve "bash" to a WSL launcher stub).
BASH = shutil.which("bash") or "bash"

# A trimmed stand-in for deploy.sh's real scaffolding: same `set`/`shopt`, the same
# on_error()/fail() pair (including the ERR trap and the BASH_SUBSHELL guard), and a
# do_apply() whose control flow is copied verbatim from deploy.sh's do_apply(), with
# `tf()` and `classify_apply_failure()` stubbed so each scenario can drive it directly.
HARNESS = r"""
set -Eeuo pipefail
shopt -s inherit_errexit

STEP=start
ERR_MSG=""
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

on_error() {
  local rc=$?
  if ((BASH_SUBSHELL > 0)); then exit "$rc"; fi
  trap - ERR
  set +e
  local msg=${ERR_MSG:-"Deployment failed at step $STEP"}
  echo "TRAP_FIRED rc=$rc msg=$msg"
  exit "$rc"
}
trap on_error ERR

fail() {
  ERR_MSG=$1
  false
}

# Stub standing in for the real classify_apply_failure.sh: recognizes one signature.
classify_apply_failure() {
  local output_file=$1 text
  text=$(cat "$output_file")
  if [[ $text == *BOOM* ]]; then
    echo "classified: recognized BOOM failure"
    return
  fi
}

# Stub standing in for `tf apply -input=false tfplan`: behavior selected by $APPLY_MODE.
tf() {
  case "$APPLY_MODE" in
    success)
      echo "apply: 3 added, 0 changed, 0 destroyed"
      return 0
      ;;
    classified)
      echo "line one of apply output"
      echo "Error: BOOM something failed"
      return 7
      ;;
    unclassified)
      echo "line one of apply output"
      echo "Error: some unrecognized failure"
      return 9
      ;;
  esac
}

# do_apply()'s apply step only (plan is skipped; irrelevant to this regression).
do_apply() {
  STEP="terraform apply"
  local rc=0
  tf apply -input=false tfplan 2>&1 | tee "$WORK/apply.out" || rc=$?
  if ((rc != 0)); then
    local classified
    classified=$(classify_apply_failure "$WORK/apply.out")
    if [[ -n $classified ]]; then
      fail "$classified"
    fi
    return "$rc"
  fi
}

do_apply
echo "REACHED_END"
"""


def _run(mode: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, APPLY_MODE=mode)
    return subprocess.run(
        [BASH, "-c", HARNESS],
        capture_output=True,
        text=True,
        env=env,
    )


def test_success_continues_and_exits_zero():
    result = _run("success")
    assert result.returncode == 0, result.stderr
    assert "REACHED_END" in result.stdout
    assert "TRAP_FIRED" not in result.stdout
    assert "apply: 3 added" in result.stdout


def test_classified_failure_fires_trap_with_specific_message():
    result = _run("classified")
    assert "REACHED_END" not in result.stdout
    assert "TRAP_FIRED" in result.stdout
    assert "classified: recognized BOOM failure" in result.stdout
    # The apply output itself must still have reached the stream (not swallowed).
    assert "BOOM something failed" in result.stdout


def test_unclassified_failure_fires_trap_with_generic_fallback():
    result = _run("unclassified")
    assert "REACHED_END" not in result.stdout
    assert "TRAP_FIRED" in result.stdout
    assert "rc=9" in result.stdout
    assert "msg=Deployment failed at step terraform apply" in result.stdout
    assert result.returncode == 9
