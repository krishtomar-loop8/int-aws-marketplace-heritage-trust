# Error-Handling Phase 1: Shared Classification Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One shared module turns a raw error detail string into a
classification (transient/retryable, permanent/misconfiguration,
needs-reauth, unknown) plus a plain-language, user-ready message — a single
source of truth that `backend/api/integrations.py`'s existing `suggest_fix()`
calls into instead of maintaining its own separate pattern list.

**Architecture:** A new `services/errors.py` module generalizes the pattern
already proven in `suggest_fix()`: a deterministic, ordered pattern-match over
a lowercased detail string. It adds an `integration: str | None` parameter —
when given, wording stays connector-specific exactly as today (verified by
keeping `suggest_fix()`'s existing test suite passing unchanged); when
omitted, the same classification markers (401/402/403/429/timeout/5xx) still
apply, with integration-agnostic wording, so later phases (the API-layer
catch-all, Bedrock runtime errors) can call it without connector-shaped
assumptions. `suggest_fix()` becomes a one-line wrapper around it.

**Tech Stack:** Python 3.13, stdlib only (no new dependencies).

**Spec:** `docs/superpowers/specs/2026-11-13-product-wide-error-handling-design.md`
(Phase 1 section, plus the shared Goals/Non-goals/Anti-goals that apply to
every phase).

## Global Constraints

- No new generic "error framework" (registries, middleware, abstract base
  classes) — a deterministic pattern-match function, same style as today's
  `suggest_fix()`, is the model (spec Non-goals).
- `services/errors.py` must never be imported by `installer/function/handler.py`
  or anything under `installer/` — that Lambda is deliberately self-contained
  (stdlib + boto3/botocore only, inlined into the CloudFormation template)
  and must gain no new dependency (spec Anti-goals). This task does not touch
  `installer/` at all; this constraint exists to catch any accidental import.
- `suggest_fix(integration: str, detail: str) -> str`'s existing public
  signature and behavior must not change — its two existing callers
  (`backend/api/handlers.py`'s health-probe path, `backend/api/connectors_admin.py`)
  and its six existing tests (`backend/api/tests/test_integrations.py`) must
  keep passing unchanged after the refactor.
- Classification order (first match wins) mirrors `suggest_fix()`'s existing
  order exactly: needs-reauth markers → 402 → 403/forbidden → 429/throttle →
  timeout/connection → 5xx → unknown default. This order already resolves the
  only realistic overlap (a detail containing both an auth marker and a 5xx
  code is classified as needs-reauth, matching today's behavior).

## Review Focus

- An empty or `None` detail string must not raise — classify it as `unknown`
  with the generic message, the same way `(detail or "").lower()` already
  guards this in `suggest_fix()`.
- Calling with `integration=None` must never hit the QuickBooks-specific or
  other connector-specific wording branches — those must only ever trigger
  when `integration` is actually given.
- A detail string matching more than one pattern (e.g. contains both an auth
  marker and "503") must resolve to exactly one classification, in the order
  stated above — not raise, not silently pick a different branch than before.
- `suggest_fix()`'s six existing tests in `test_integrations.py` must pass
  **unchanged** (no test edits) after the refactor — this is the regression
  guard that proves the refactor preserved behavior.
- Nothing in this task's diff touches any file under `installer/` — confirmed
  by the diff itself, not just by description, before this task is marked done.

---

### Task 1: `services/errors.py` — classification + message catalog

**Files:**
- Create: `services/errors.py`
- Test: `services/tests/test_errors.py`

**Interfaces:**
- Produces: `ErrorClass` (a `str` enum: `TRANSIENT`, `PERMANENT`, `NEEDS_REAUTH`,
  `UNKNOWN`); `ClassifiedError` (a frozen dataclass: `error_class: ErrorClass`,
  `message: str`); `classify(detail: str, *, integration: str | None = None) ->
  ClassifiedError`. Task 2 imports all three by these exact names from
  `services.errors`.

- [ ] **Step 1: Write the failing tests**

```python
# services/tests/test_errors.py
from services.errors import ErrorClass, classify


def test_classify_empty_detail_is_unknown() -> None:
    result = classify("")
    assert result.error_class == ErrorClass.UNKNOWN

def test_classify_none_detail_is_unknown() -> None:
    result = classify(None)  # type: ignore[arg-type]
    assert result.error_class == ErrorClass.UNKNOWN

def test_classify_quickbooks_token_is_reauthorize() -> None:
    result = classify("HttpError: QuickBooks token refresh failed: 400", integration="quickbooks")
    assert result.error_class == ErrorClass.NEEDS_REAUTH
    assert "re-authorize" in result.message.lower()
    assert "quickbooks" in result.message.lower()

def test_classify_generic_auth_without_integration() -> None:
    result = classify("HttpError: 401 Unauthorized")
    assert result.error_class == ErrorClass.NEEDS_REAUTH
    assert "quickbooks" not in result.message.lower()
    assert "connector" not in result.message.lower()

def test_classify_billing_hold_is_permanent() -> None:
    result = classify("HttpError: 402 Payment Required")
    assert result.error_class == ErrorClass.PERMANENT
    assert "402" in result.message

def test_classify_forbidden_is_permanent() -> None:
    result = classify("HttpError: 403 Forbidden")
    assert result.error_class == ErrorClass.PERMANENT

def test_classify_rate_limit_is_transient() -> None:
    result = classify("HttpError: 429 Too Many Requests")
    assert result.error_class == ErrorClass.TRANSIENT

def test_classify_timeout_is_transient() -> None:
    result = classify("ReadTimeout: read timed out")
    assert result.error_class == ErrorClass.TRANSIENT

def test_classify_server_error_is_transient() -> None:
    result = classify("HttpError: 503 ServiceUnavailable")
    assert result.error_class == ErrorClass.TRANSIENT

def test_classify_unmatched_detail_is_unknown() -> None:
    result = classify("ValueError: something odd")
    assert result.error_class == ErrorClass.UNKNOWN

def test_classify_auth_marker_wins_over_5xx_when_both_present() -> None:
    # Priority order: auth markers are checked first, matching suggest_fix()'s
    # existing order, even when a 5xx code also appears in the same detail.
    result = classify("HttpError: 401 then retried and got 503")
    assert result.error_class == ErrorClass.NEEDS_REAUTH

def test_classify_without_integration_never_mentions_a_specific_connector() -> None:
    for detail in (
        "HttpError: QuickBooks token refresh failed: 400",
        "HttpError: 402 Payment Required",
        "HttpError: 403 Forbidden",
        "HttpError: 429 Too Many Requests",
        "ReadTimeout: read timed out",
        "HttpError: 503 ServiceUnavailable",
        "ValueError: something odd",
    ):
        result = classify(detail)
        assert "quickbooks" not in result.message.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest services/tests/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.errors'`

- [ ] **Step 3: Implement `services/errors.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    NEEDS_REAUTH = "needs_reauth"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedError:
    error_class: ErrorClass
    message: str


def classify(detail: str, *, integration: str | None = None) -> ClassifiedError:
    ...
```

Port `suggest_fix()`'s exact pattern-match order and marker lists
(`backend/api/integrations.py`) into `classify()`'s body: for each branch,
return `ClassifiedError(error_class=<matching class>, message=<text>)`. When
`integration` is given, use today's exact connector-aware wording (including
the `integration == "quickbooks"` special case inside the needs-reauth
branch) so `suggest_fix()`'s six existing tests keep passing byte-for-byte
once Task 2 wires it up. When `integration` is `None`, use the generic,
connector-agnostic wording the tests above assert on (no "the connector",
no "the provider", no connector name) for the same classification markers.
Guard `detail` the same way `suggest_fix()` does: `(detail or "").lower()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest services/tests/test_errors.py -v`
Expected: PASS (all 12 tests)

- [ ] **Step 5: Commit**

```bash
git add services/errors.py services/tests/test_errors.py
git commit -m "feat: add the shared error-classification catalog"
```

---

### Task 2: Refactor `suggest_fix()` to call the catalog

**Files:**
- Modify: `backend/api/integrations.py`
- Test: `backend/api/tests/test_integrations.py` (no edits — this is the
  regression guard; it must pass unchanged)

**Interfaces:**
- Consumes: Task 1's `services.errors.classify` (exact name and signature as
  defined there).
- Produces: nothing new — `suggest_fix(integration: str, detail: str) -> str`
  keeps its existing public signature; this task only changes its
  implementation.

- [ ] **Step 1: Confirm the regression baseline passes before changing anything**

Run: `python -m pytest backend/api/tests/test_integrations.py -v`
Expected: PASS (all 6 existing `suggest_fix` tests, before any change)

- [ ] **Step 2: Replace `suggest_fix()`'s body in `backend/api/integrations.py`**

```python
def suggest_fix(integration: str, detail: str) -> str:
    """Map a connector error detail to a plain-language suggested fix for staff.

    Thin wrapper over the shared catalog (``services.errors.classify``) —
    kept as its own function because its two existing callers (the health
    probe and connector-connect admin path) only need the message string.
    """
    from services.errors import classify

    return classify(detail, integration=integration).message
```

Remove the now-dead inline pattern-match body (the `auth_markers`/`402`/`403`/
`429`/timeout/`5xx` branches) — it lives in `services/errors.py` now. Keep the
module-level docstring in `backend/api/integrations.py` as-is (it documents
the health-probe system, not just this function).

- [ ] **Step 3: Run the regression suite**

Run: `python -m pytest backend/api/tests/test_integrations.py -v`
Expected: PASS (same 6 tests, unedited, now exercising the refactored
implementation)

- [ ] **Step 4: Run the full backend test suite**

Run: `python -m pytest backend/ services/ -q`
Expected: PASS, no regressions anywhere else (both of `suggest_fix()`'s
callers — `backend/api/handlers.py`'s health-probe path and
`backend/api/connectors_admin.py` — are exercised by the existing suite).

- [ ] **Step 5: Confirm no `installer/` file changed**

Run: `git diff --stat main -- installer/`
Expected: empty output.

- [ ] **Step 6: Commit**

```bash
git add backend/api/integrations.py
git commit -m "refactor: suggest_fix delegates to the shared error catalog"
```
