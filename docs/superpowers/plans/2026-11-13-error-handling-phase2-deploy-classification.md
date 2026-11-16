# Error-Handling Phase 2: Deploy-Time Failure Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `terraform apply` fails during a customer's deployment for a
recognizable AWS reason (service quota/limit, account billing hold, or an
IAM permissions problem), the CloudFormation failure reason the customer
sees names that reason and what to do about it — instead of today's generic
"Deployment failed at step terraform apply" plus a CodeBuild log link.

**Architecture:** `installer/deploy.sh` already has the plumbing for this —
`fail(msg)` sets `ERR_MSG`, which `on_error`'s trap writes to the
`/aphrodite/installer/last-error` SSM parameter instead of the generic
fallback message (the Bedrock pre-flight check already uses exactly this
pattern). `do_apply()`'s `tf apply` call is the only step that doesn't. This
phase adds a new, independently testable function,
`classify_apply_failure()`, in a new sourced file
(`installer/classify_apply_failure.sh`), and changes `do_apply()` to capture
`tf apply`'s output, check its exit code manually instead of relying on the
bare `ERR` trap, and call `fail()` with a classified message when one
applies — falling through to today's generic message otherwise. This phase
is standalone: it does not import anything from `services/` (installer code
stays self-contained, per the design doc's Anti-goals) and the catalog from
Phase 1 is not used here.

**Tech Stack:** Bash (the existing `installer/deploy.sh` is `set -Eeuo
pipefail`), Python/pytest (existing test convention for installer logic —
`installer/tests/*.py` already shells out to/reads `deploy.sh` rather than
unit-testing bash with a bash test framework).

**Spec:** `docs/superpowers/specs/2026-11-13-product-wide-error-handling-design.md`
(Phase 2 section, plus the shared Goals/Non-goals/Anti-goals/Constraints
that apply to every phase).

## Global Constraints

- No new dependency on `services/` or any Python backend module from
  `installer/deploy.sh` or `installer/classify_apply_failure.sh` — the
  installer stays self-contained (spec Anti-goals). This phase is pure
  bash plus the `python3 -c` one-liner already used elsewhere in
  `deploy.sh` for JSON encoding (not a new pattern).
- `deploy.sh` is invoked as `bash installer/deploy.sh` with the release
  bundle root as the working directory (confirmed in
  `installer/installer.yaml:332`) — any new file is sourced with a
  bundle-root-relative path (`source installer/classify_apply_failure.sh`),
  matching the existing `tf() { bin/terraform -chdir=terraform "$@"; }`
  convention of CWD-relative paths, not `$BASH_SOURCE`-relative ones.
- `do_apply()`'s existing fallback behavior must be preserved exactly for
  any failure `classify_apply_failure()` doesn't recognize: the generic
  `"Deployment failed at step terraform apply"` message, written via the
  existing `on_error` trap, with the same non-zero exit code propagated to
  CodeBuild. This phase only adds a *more specific* message for recognized
  cases — it must never swallow a failure or change exit-code behavior for
  an unrecognized one.
- `set -Eeuo pipefail` stays in effect for the rest of the script — the new
  code in `do_apply()` must not weaken error handling for anything other
  than the one `tf apply` invocation it wraps.
- v1 classification set (this phase's own call, per the spec's "What's left
  to the builder"): service quota/limit (`LimitExceededException`,
  `ServiceQuotaExceededException`), an AWS account billing/verification hold
  (`account is currently blocked` — AWS's own documented wording for a
  new-account fraud/billing hold), and an IAM/permissions problem
  (`AccessDenied`, `UnauthorizedOperation`, `is not authorized to perform`).
  Anything else falls through to the existing generic message unchanged.

## Review Focus

- `tf apply` succeeding must behave exactly as today — no new output
  capture artifact changes `do_apply()`'s success path, and `STEP` /
  `ERR_MSG` state after a successful apply must not leak into any later
  step's error reporting.
- `tf apply` failing with *none* of the three recognized signatures in its
  output must still produce the existing generic
  `"Deployment failed at step terraform apply"` message and a non-zero
  exit — not a swallowed failure, not an empty/blank `ERR_MSG`.
- `tf apply`'s original output (stdout+stderr) must still reach the
  CodeBuild log the way it does today — a customer/support engineer
  debugging via the log link in the generic-failure case must not lose
  visibility into the raw terraform output.
- A single `tf apply` failure output containing more than one recognized
  signature (e.g. both a quota message and an unrelated IAM warning in the
  same log) must resolve to exactly one classified message, deterministically
  ordered — not raise, not concatenate conflicting advice.
- `classify_apply_failure()` must be callable and testable in complete
  isolation from a real `terraform apply` run (no AWS calls, no CodeBuild
  environment) — this is the thing that makes this phase's behavior
  verifiable at all, since `deploy.sh` itself has no end-to-end test
  harness (confirmed: existing `installer/tests/*.py` only pattern-check or
  stub the script, never execute its real control flow).

---

### Task 1: `installer/classify_apply_failure.sh` — the classification function

**Files:**
- Create: `installer/classify_apply_failure.sh`
- Test: `installer/tests/test_classify_apply_failure.py`

**Interfaces:**
- Produces: a bash function `classify_apply_failure` with signature
  `classify_apply_failure <output-file-path>` — reads the named file's
  contents and prints exactly one of three classified messages to stdout,
  or prints nothing (empty stdout) if none of the three patterns match.
  Task 2 sources this file and calls `classify_apply_failure` by this exact
  name.

- [ ] **Step 1: Write the failing tests**

```python
# installer/tests/test_classify_apply_failure.py
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer" / "classify_apply_failure.sh"


def _classify(output_text: str) -> str:
    tmp = ROOT / "installer" / "tests" / "_tmp_apply_output.txt"
    tmp.write_text(output_text)
    try:
        result = subprocess.run(
            ["bash", "-c", f"source {SCRIPT} && classify_apply_failure {tmp}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    finally:
        tmp.unlink(missing_ok=True)


def test_quota_limit_exceeded_is_classified():
    msg = _classify("Error: creating Lambda Function: LimitExceededException: ...")
    assert "quota" in msg.lower() or "limit" in msg.lower()
    assert "retry" in msg.lower() or "request" in msg.lower()

def test_service_quota_exceeded_exception_is_classified():
    msg = _classify("Error: ServiceQuotaExceededException: The maximum number of...")
    assert "quota" in msg.lower() or "limit" in msg.lower()

def test_account_billing_hold_is_classified():
    msg = _classify("Error: Your account is currently blocked. Contact aws-verification@amazon.com")
    assert "billing" in msg.lower() or "account" in msg.lower()

def test_iam_access_denied_is_classified():
    msg = _classify("Error: creating IAM Role: AccessDenied: User is not authorized to perform iam:CreateRole")
    assert "permission" in msg.lower() or "iam" in msg.lower()

def test_unauthorized_operation_is_classified():
    msg = _classify("Error: UnauthorizedOperation: You are not authorized to perform this operation")
    assert "permission" in msg.lower() or "iam" in msg.lower()

def test_unrecognized_failure_returns_empty():
    msg = _classify("Error: creating S3 Bucket: BucketAlreadyExists: the requested bucket name is not available")
    assert msg == ""

def test_empty_output_returns_empty():
    msg = _classify("")
    assert msg == ""

def test_quota_wins_when_multiple_signatures_present():
    # Priority order: quota/limit is checked first, deterministically, when a
    # failure output happens to contain more than one recognized signature.
    msg = _classify(
        "Error: LimitExceededException: too many functions\n"
        "Warning: AccessDenied on an unrelated describe call"
    )
    assert "quota" in msg.lower() or "limit" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest installer/tests/test_classify_apply_failure.py -v`
Expected: FAIL (`installer/classify_apply_failure.sh` does not exist yet,
`bash -c` exits non-zero, `subprocess.run(..., check=True)` raises)

- [ ] **Step 3: Implement `installer/classify_apply_failure.sh`**

```bash
#!/usr/bin/env bash
# Classifies a failed `terraform apply`'s captured output against a small set
# of recognized AWS deploy-time failure signatures, so the CloudFormation
# failure reason names the real cause instead of a generic message (same
# idea as deploy.sh's Bedrock pre-flight check, generalized to terraform
# apply). Sourced by deploy.sh; never executed standalone in production.

classify_apply_failure() {
  local output_file=$1 text
  text=$(cat "$output_file")

  if [[ $text == *LimitExceededException* || $text == *ServiceQuotaExceededException* ]]; then
    echo "This AWS account has hit a service quota/limit while deploying (LimitExceededException/ServiceQuotaExceededException). Request a quota increase for the affected service in the Service Quotas console, then retry the stack update."
    return
  fi
  if [[ $text == *"account is currently blocked"* ]]; then
    echo "AWS has placed a billing/verification hold on this account (\"account is currently blocked\"). Resolve this with AWS Billing support, then retry the stack update."
    return
  fi
  if [[ $text == *AccessDenied* || $text == *UnauthorizedOperation* || $text == *"is not authorized to perform"* ]]; then
    echo "The deploying IAM role/user lacks a permission terraform needs (AccessDenied/UnauthorizedOperation). Check the IAM policy attached to the role running this deployment, then retry the stack update."
    return
  fi
}
```

Match the priority order the tests pin: quota/limit checked first, then the
billing hold, then IAM/permissions — so a failure output containing more
than one signature resolves deterministically. Keep each branch's message
specific and actionable (name the AWS error type, name the console/support
channel, end with "then retry the stack update" to match this script's
existing Bedrock pre-flight message's call-to-action style).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest installer/tests/test_classify_apply_failure.py -v`
Expected: PASS (all 8 tests)

- [ ] **Step 5: Commit**

```bash
git add installer/classify_apply_failure.sh installer/tests/test_classify_apply_failure.py
git commit -m "feat: classify recognized AWS failures in a failed terraform apply"
```

---

### Task 2: Wire classification into `do_apply()`

**Files:**
- Modify: `installer/deploy.sh`
- Modify: `installer/tests/test_bundle_files.py` (add one consistency check)

**Interfaces:**
- Consumes: Task 1's `classify_apply_failure <output-file-path>` (exact
  name, sourced via `source installer/classify_apply_failure.sh`).
- Produces: nothing new — this is the integration point; no later task
  consumes anything from here.

- [ ] **Step 1: Source the new file**

Add `source installer/classify_apply_failure.sh` near the top of
`installer/deploy.sh`, after the existing helper function definitions
(`fail`, `require`) and before `do_apply()`'s definition — matching the
script's existing top-to-bottom definitions-then-steps structure.

- [ ] **Step 2: Capture `tf apply`'s output and exit code manually**

Replace `do_apply()`'s body:

```bash
do_apply() {
  STEP="terraform plan"
  tf plan -input=false -out=tfplan
  STEP="terraform apply"
  set +e
  tf apply -input=false tfplan >"$WORK/apply.out" 2>&1
  local rc=$?
  set -e
  cat "$WORK/apply.out"
  if ((rc != 0)); then
    local classified
    classified=$(classify_apply_failure "$WORK/apply.out")
    if [[ -n $classified ]]; then
      fail "$classified"
    fi
    return "$rc"
  fi
}
```

`cat "$WORK/apply.out"` (unconditional, both on success and failure)
preserves today's CodeBuild log visibility into the raw terraform output —
confirm this by checking `tf plan`'s own call just above has no output
redirection today (it doesn't; only `apply` needs this change, since only
`apply` is being wrapped). On the classified-failure path, `fail "$classified"`
sets `ERR_MSG` and calls `false`, which the still-armed `ERR` trap (`trap -
ERR` is never run here — only `on_error` disarms it, and `on_error` is only
entered via the trap firing) catches exactly as it does for `fail()`'s other
existing caller (the Bedrock pre-flight step). On the unclassified-failure
path, `return "$rc"` is itself a non-zero return under `set -Eeuo
pipefail`'s `errexit`, which still fires the same `on_error` trap — with
`ERR_MSG` unset, producing today's unchanged generic message.

- [ ] **Step 3: Add a consistency check**

In `installer/tests/test_bundle_files.py`, add:

```python
def test_deploy_script_sources_the_classifier():
    script = (INSTALLER / "deploy.sh").read_text()
    assert "source installer/classify_apply_failure.sh" in script
```

This matches the file's existing style (`test_deploy_script_never_bypasses_governance`)
of a text-pattern check against `deploy.sh`, since there is no harness that
executes the real script end-to-end.

- [ ] **Step 4: Run the test suite**

Run: `python -m pytest installer/tests/ -q`
Expected: PASS, no regressions (includes Task 1's 8 new tests plus this
task's 1 new test plus all pre-existing installer tests).

- [ ] **Step 5: Manually verify the bash control flow (no AWS calls)**

Since there is no harness for executing `deploy.sh` end-to-end, verify
`do_apply()`'s new structure directly: write a tiny standalone bash snippet
(not committed — a scratch check, same spirit as this codebase's existing
"temporarily force X, verify, then revert" manual-verification steps) that
sources `installer/deploy.sh` is not practical (it has top-level side
effects), so instead: copy `do_apply()`'s new body into a throwaway test
script with a fake `tf()` function that `echo`s a message to stdout and
exits 1, source `classify_apply_failure.sh`, and confirm (a) the fake
output reaches stdout, (b) `ERR_MSG` ends up set to the classified message
when the fake output contains a recognized signature, (c) `ERR_MSG` stays
empty when it doesn't. Do not leave this throwaway script in the repo —
`git status` must be clean except for this task's two files before
committing.

- [ ] **Step 6: Commit**

```bash
git add installer/deploy.sh installer/tests/test_bundle_files.py
git commit -m "feat: wire deploy-time failure classification into terraform apply"
```
