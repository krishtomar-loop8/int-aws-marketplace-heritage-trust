# Error Handling Phase 5 — Unattended-job failure visibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sure none of the three unattended, EventBridge-scheduled jobs (Quarterly Report Draft Generator, Monthly AML Name Export, Sanctions Refresh) can ever fail with zero trace reaching a human, and that the Sanction Screener tells staff when it screened against a possibly-stale watchlist bundle without ever blocking the screening.

**Architecture:** Three small, independent backend fixes (close a pre-loop exception gap in two existing jobs; add a brand-new SNS alerting path to a third job that has none today) plus one new Terraform SNS topic, plus a backend+frontend pair adding a non-blocking staleness banner to the Sanction Screener. No new shared framework — every fix extends the exact `notify()`/SNS-publish pattern each job (or its siblings) already uses.

**Tech Stack:** Python 3.13 (backend Lambdas, pytest), Terraform (AWS SNS), TypeScript/React (frontend, no new test framework — this repo has none for the frontend; verified via `tsc -b` + `npm run build`).

**Spec:** `docs/superpowers/specs/2026-11-13-product-wide-error-handling-design.md` (Phase 5 section, plus the shared Goals/Non-goals/Anti-goals/Constraints that apply project-wide).

## Global Constraints

- **Sanction Screener stale-bundle behavior: warn and allow, never block.** A screening must never be prevented from completing because the bundle is stale — only flagged with a banner.
- **No new generic error-handling framework.** Extend each job's existing `notify()`-style SNS-publish pattern; do not introduce a shared abstraction across the three unattended jobs.
- **No Claude/Co-Authored-By attribution** in any commit message (CLAUDE.md).
- **No local deployment.** `terraform validate`/`fmt` are fine locally (read-only); a real `terraform apply` goes through CI, not this plan's execution.
- **Every phase ships as its own branch and PR**, reviewed via subagent-driven-development: per-task review, a final whole-branch review, one fix wave, one scoped re-review — matching Phases 1-4.

## Review Focus

- **A `notify_failure`/`notify` SNS publish that itself raises (e.g. the topic was deleted, a permissions error) must never replace or swallow the original failure's exception.** The except block calling it must not let a secondary SNS error mask the real one. → pinned in Tasks 1, 2, and 3's own test steps.
- **The new top-level try/except in Quarterly Report must not change existing, correct behavior for a batch that completes with some per-matter failures already isolated.** That case must still call the normal `deps.notify(period, results, ...)` (the "N succeeded, M failed" summary), not the new failure-path notification — only a failure *before* or *around* the loop (discovery, config resolution, the final `deps.notify` call itself) should route to the new path. → pinned in Task 1.
- **Sanctions Refresh: an S3 write failure *after* a fully successful fetch** (all three sources returned data, but `put_object` itself fails) must still reach the new alerting path, not just the "incomplete fetch" abort case. → pinned in Task 3.
- **Sanction Screener staleness: a timezone-naive `generated_at` string** (no UTC offset — plausible from a hand-edited or older bundle) must not raise when compared against an aware `datetime.now(UTC)`. → pinned in Task 5.
- **Sanction Screener staleness: the exact threshold boundary.** A bundle exactly at the chosen threshold and one day past it must land on the correct side of "stale" — pins the comparison operator, not just the general direction. → pinned in Task 5.

---

### Task 1: Quarterly Report — close the pre-loop failure gap

**Files:**
- Modify: `backend/lambdas/quarterly_report/handler.py`
- Test: `backend/lambdas/quarterly_report/tests/test_handler.py`

**Interfaces:**
- Consumes: nothing new — reuses the existing `Deps.notify` callable and the SNS client/`topic_arn` already built in `_build_deps()`.
- Produces: a new `Deps.notify_failure(context: str, error: str) -> None` field, required (no default), constructed in `_build_deps()` as a sibling of the existing `notify` closure (same `sns`/`topic_arn` guard: a no-op when `topic_arn` is unset, matching `notify`'s own existing behavior). No other task consumes this — Task 2 defines its *own* `notify_failure` independently (same shape, different message, mirroring how `notify` itself is not shared between jobs today).

- [ ] **Step 1: Write the failing tests**

Add `import pytest` to this test file's imports (not currently imported there). In `backend/lambdas/quarterly_report/tests/test_handler.py`, change `_install` to also build and return a `failures` list, mirroring the existing `notifications` pattern:

```python
def _install(
    monkeypatch, generator, storage, *, delivery=None, sleep=None, delay=0.0, **deps_kwargs
):
    notifications = []
    failures = []
    extra = {"sleep": sleep} if sleep is not None else {}
    deps = Deps(
        generator=generator,
        storage=storage,
        notify=lambda p, r, flagged=None: notifications.append((p, r, flagged)),
        notify_failure=deps_kwargs.pop(
            "notify_failure", lambda context, error: failures.append((context, error))
        ),
        delivery=delivery,
        delay_seconds=delay,
        **extra,
        **deps_kwargs,
    )
    monkeypatch.setattr(H, "_DEPS", deps)
    return notifications, failures
```

Every existing call site (`notifications = _install(...)`) becomes `notifications, _ = _install(...)`. Then add:

```python
def test_handler_calls_notify_failure_when_discovery_raises(monkeypatch):
    gen, store = FakeGenerator(), FakeStorage()
    notifications, failures = _install(monkeypatch, gen, store)

    def boom(deps):
        raise RuntimeError("Clio is down")

    monkeypatch.setattr(H, "_discover", boom)

    with pytest.raises(RuntimeError, match="Clio is down"):
        H.handler({"discover": True})

    assert notifications == []
    assert len(failures) == 1
    assert "Clio is down" in failures[0][1]


def test_notify_failure_raising_does_not_mask_the_original_exception(monkeypatch):
    gen, store = FakeGenerator(), FakeStorage()

    def failing_notify_failure(context, error):
        raise RuntimeError("sns down")

    _install(monkeypatch, gen, store, notify_failure=failing_notify_failure)
    monkeypatch.setattr(H, "_discover", lambda deps: (_ for _ in ()).throw(RuntimeError("original")))

    with pytest.raises(RuntimeError, match="original"):
        H.handler({"discover": True})


def test_per_matter_failure_still_uses_normal_notify_not_notify_failure(monkeypatch):
    gen, store = FakeGenerator(fail_for={"100002"}), FakeStorage()
    notifications, failures = _install(monkeypatch, gen, store)

    H.handler({"matter_ids": ["100001", "100002"], "period": {"year": 2026, "quarter": 1}})

    assert len(notifications) == 1
    assert failures == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/lambdas/quarterly_report/tests/test_handler.py -k "notify_failure or per_matter_failure_still" -v`
Expected: FAIL — `Deps` has no `notify_failure` field/argument yet (`TypeError: __init__() got an unexpected keyword argument 'notify_failure'`).

- [ ] **Step 3: Add `notify_failure` to `Deps` and `_build_deps()`, and wrap `handler()`'s body**

In `Deps` (the dataclass), add `notify_failure: Callable[[str, str], None]` as a new required field (next to the existing `notify` field).

In `_build_deps()`, define a sibling closure next to `notify`:

```python
def notify_failure(context: str, error: str) -> None:
    if not sns:
        return
    sns.publish(
        TopicArn=topic_arn,
        Subject=f"Quarterly report run failed — {context}",
        Message=f"The quarterly report generator failed before completing.\n\n{context}: {error}",
    )
```

Pass it into the returned `Deps(...)` call alongside `notify`.

In `handler()`, restructure so `base_deps = _get_deps()` is read first (unchanged — a `_get_deps()`/cold-start failure itself has no SNS client to alert through and stays out of scope, same as every sibling job), then wrap everything from `deps = _with_current_config(base_deps)` through the final `return out` in one `try`, with:

```python
except Exception as exc:
    logger.exception("quarterly report run failed before completion")
    try:
        base_deps.notify_failure("quarterly report run", str(exc))
    except Exception:  # noqa: BLE001 - never let the alert itself mask the real failure
        logger.exception("notify_failure itself failed")
    raise
```

The existing early returns inside that span (`discover_only`, the no-`matter_ids` case) are not exceptions, so they pass through the `try` unaffected; the existing per-matter isolation inside `_generate_one` is untouched, so a batch with some per-matter failures still falls through to the existing `deps.notify(period, results, flagged=flagged)` call and returns normally — it never reaches the new `except`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/lambdas/quarterly_report/tests/test_handler.py -v`
Expected: PASS, all tests (every pre-existing test plus the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add backend/lambdas/quarterly_report/handler.py backend/lambdas/quarterly_report/tests/test_handler.py
git commit -m "fix: alert on quarterly report failures that occur before per-matter processing"
```

---

### Task 2: AML Export — close the pre-loop failure gap

**Files:**
- Modify: `backend/lambdas/aml_export/handler.py`
- Test: `backend/lambdas/aml_export/tests/test_handler.py`

**Interfaces:**
- Consumes: nothing from Task 1 — this job's `Deps`/`_build_deps()`/`handler()` are entirely separate files with no shared code, by this project's own Anti-goals (no new shared module between jobs).
- Produces: a new `Deps.notify_failure(context: str, error: str) -> None` field on *this* module's `Deps`, independent of Task 1's.

- [ ] **Step 1: Write the failing tests**

Update the existing `_install` helper in `backend/lambdas/aml_export/tests/test_handler.py` to also accept and wire an optional `notify_failure`, and to return it alongside the existing `notifications` list:

```python
def _install(monkeypatch, estateably, clio, storage, config=None, notify_failure=None):
    notifications: list[tuple[dict[str, str], dict[str, Any]]] = []
    failures: list[tuple[str, str]] = []
    deps = Deps(
        estateably=estateably,
        clio=clio,
        storage=storage,
        notify=lambda keys, result: notifications.append((keys, result)),
        notify_failure=notify_failure or (lambda context, error: failures.append((context, error))),
        config=config,
    )
    monkeypatch.setattr(H, "_DEPS", deps)
    return notifications, failures
```

Every existing call site (`notifications = _install(...)`) becomes `notifications, _ = _install(...)`. Then add:

```python
def test_handler_calls_notify_failure_when_load_prior_matter_numbers_raises(monkeypatch):
    class FailingStorage(FakeStorage):
        def load_prior_matter_numbers(self, now):
            raise RuntimeError("S3 listing failed")

    notifications, failures = _install(monkeypatch, FakeEstateably(), FakeClio(), FailingStorage())

    with pytest.raises(RuntimeError, match="S3 listing failed"):
        H.handler({})

    assert notifications == []
    assert len(failures) == 1
    assert "S3 listing failed" in failures[0][1]


def test_notify_failure_raising_does_not_mask_the_original_exception(monkeypatch):
    class FailingStorage(FakeStorage):
        def load_prior_matter_numbers(self, now):
            raise RuntimeError("original failure")

    def failing_notify_failure(context, error):
        raise RuntimeError("sns down")

    _install(
        monkeypatch,
        FakeEstateably(),
        FakeClio(),
        FailingStorage(),
        notify_failure=failing_notify_failure,
    )

    with pytest.raises(RuntimeError, match="original failure"):
        H.handler({})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/lambdas/aml_export/tests/test_handler.py -k notify_failure -v`
Expected: FAIL — `notify_failure` not yet a recognized `Deps` field.

- [ ] **Step 3: Add `notify_failure` to `Deps` and `_build_deps()`, and wrap `handler()`'s body**

Same shape as Task 1: add `notify_failure: Callable[[str, str], None]` to `Deps`; in `_build_deps()`, a sibling closure to the existing `notify`:

```python
def notify_failure(context: str, error: str) -> None:
    if not sns:
        return
    sns.publish(
        TopicArn=topic_arn,
        Subject=f"AML export run failed — {context}",
        Message=f"The monthly AML name export failed before completing.\n\n{context}: {error}",
    )
```

In `handler()`, wrap everything from `quality.configure(...)` through the final `return {...}` in one `try`, with the same shape of `except Exception` block as Task 1 (log, best-effort `deps.notify_failure("AML export run", str(exc))` itself wrapped in its own `try/except` so it can't mask the original, then `raise`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/lambdas/aml_export/tests/test_handler.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add backend/lambdas/aml_export/handler.py backend/lambdas/aml_export/tests/test_handler.py
git commit -m "fix: alert on AML export failures that occur before completion"
```

---

### Task 3: Sanctions Refresh — add SNS failure alerting

**Files:**
- Modify: `backend/lambdas/sanctions_refresh/handler.py`
- Test: `backend/lambdas/sanctions_refresh/tests/test_handler.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-2. Reads a new env var `APHRODITE_NOTIFY_TOPIC_ARN`, following the exact naming convention Quarterly Report and AML Export already use for their own topics (confirmed identical name reused per-Lambda, since each Lambda has its own topic ARN in its own environment — no cross-job collision).
- Produces: the env var name `APHRODITE_NOTIFY_TOPIC_ARN` that Task 4's Terraform must set on this specific Lambda's `environment.variables` block (Task 4 depends on this task's naming choice, not the reverse).

- [ ] **Step 1: Update the existing test fixtures for the new required `Deps` field**

In `backend/lambdas/sanctions_refresh/tests/test_handler.py`, change `_inject` to also take and wire a `notify_failure` fake:

```python
def _inject(monkeypatch, s3, data, notify_failure=None):
    monkeypatch.setattr(fetcher, "fetch_all", lambda force_http=False: data)
    deps = h.Deps(
        s3=s3,
        bucket="ref",
        key="sanctions/bundle.json",
        kms_key_id=None,
        notify_failure=notify_failure or (lambda *a: None),
    )
    monkeypatch.setattr(h, "_DEPS", deps)
```

The two existing tests (`test_refresh_writes_bundle`, `test_refresh_aborts_on_partial_fetch`) need no other change — they'll pass once `Deps` accepts the new field with the no-op default supplied here.

- [ ] **Step 2: Write the new failing tests**

```python
def test_refresh_notifies_on_partial_fetch(monkeypatch):
    s3 = FakeS3()
    failures = []
    _inject(
        monkeypatch,
        s3,
        {"un_list": [], "sema_list": [{"full_name": "Y"}], "ps_list": [{"full_name": "Z"}]},
        notify_failure=lambda error, counts: failures.append((error, counts)),
    )

    with pytest.raises(RuntimeError):
        h.handler({}, None)

    assert len(failures) == 1
    error, counts = failures[0]
    assert counts == {"un": 0, "sema": 1, "ps": 1, "total": 2}
    assert "incomplete" in error


def test_refresh_notifies_on_s3_write_failure_after_full_fetch(monkeypatch):
    class FailingS3(FakeS3):
        def put_object(self, **kwargs):
            raise RuntimeError("S3 unavailable")

    s3 = FailingS3()
    failures = []
    _inject(
        monkeypatch,
        s3,
        {
            "un_list": [{"full_name": "X"}],
            "sema_list": [{"full_name": "Y"}],
            "ps_list": [{"full_name": "Z"}],
        },
        notify_failure=lambda error, counts: failures.append((error, counts)),
    )

    with pytest.raises(RuntimeError, match="S3 unavailable"):
        h.handler({}, None)

    assert len(failures) == 1
    assert failures[0][1] == {"un": 1, "sema": 1, "ps": 1, "total": 3}  # full counts were known


def test_notify_failure_raising_does_not_mask_the_original_exception(monkeypatch):
    s3 = FakeS3()
    _inject(
        monkeypatch,
        s3,
        {"un_list": [], "sema_list": [], "ps_list": []},
        notify_failure=lambda *a: (_ for _ in ()).throw(RuntimeError("sns down")),
    )

    with pytest.raises(RuntimeError, match="incomplete"):
        h.handler({}, None)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest backend/lambdas/sanctions_refresh/tests/test_handler.py -v`
Expected: FAIL (`Deps.__init__()` doesn't accept `notify_failure` yet).

- [ ] **Step 4: Add `notify_failure` to `Deps`, build it in `_build_deps()`, and wrap `handler()`'s body**

Add `from collections.abc import Callable` to the imports. Add `notify_failure: Callable[[str, dict[str, int] | None], None]` to the `Deps` dataclass.

In `_build_deps()`, construct an SNS client exactly the way Quarterly Report/AML Export already do (`topic_arn = os.environ.get("APHRODITE_NOTIFY_TOPIC_ARN")`, `sns = boto3.client("sns", region_name=REGION) if topic_arn else None`), and define:

```python
def notify_failure(error: str, counts: dict[str, int] | None) -> None:
    if not sns:
        return
    labels = {"un": "UN Consolidated", "sema": "Canada SEMA", "ps": "Public Safety Canada"}
    detail = ""
    if counts:
        lines = "\n".join(f"- {label}: {counts.get(key, 0)} entries" for key, label in labels.items())
        detail = f"\n\n{lines}"
    sns.publish(
        TopicArn=topic_arn,
        Subject="Sanctions watchlist refresh failed",
        Message=f"The monthly sanctions watchlist refresh did not complete.\n\n{error}{detail}",
    )
```

Pass `notify_failure` into `Deps(...)`.

Restructure `handler()`: initialize `counts: dict[str, int] | None = None` before the `try`; move everything from the `fetcher.fetch_all(...)` call through `deps.s3.put_object(...)` and the final `return {...}` inside the `try` (assigning to the outer `counts` as soon as it's computed, so it's available to the `except` whether the failure happens at the "incomplete fetch" `raise` or later at the S3 write); in `except Exception as exc`, log, then call `deps.notify_failure(str(exc), counts)` wrapped in its own inner `try/except` (same masking protection as Tasks 1-2), then `raise`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/lambdas/sanctions_refresh/tests/test_handler.py -v`
Expected: PASS, all tests (2 pre-existing + 4 new).

- [ ] **Step 6: Commit**

```bash
git add backend/lambdas/sanctions_refresh/handler.py backend/lambdas/sanctions_refresh/tests/test_handler.py
git commit -m "feat: add SNS failure alerting to the sanctions-refresh job"
```

---

### Task 4: Sanctions Refresh — Terraform SNS topic

**Files:**
- Modify: `terraform/modules/sanctions_refresh/main.tf`
- Modify: `terraform/modules/sanctions_refresh/variables.tf`
- Modify: `terraform/modules/sanctions_refresh/outputs.tf`
- Modify: `terraform/variables.tf`
- Modify: `terraform/main.tf`

**Interfaces:**
- Consumes: Task 3's env var name `APHRODITE_NOTIFY_TOPIC_ARN` (must be set on `aws_lambda_function.refresh`'s `environment.variables`, exactly as named there — no other task reads this).
- Produces: nothing consumed by a later task in this plan.

- [ ] **Step 1: Add the SNS topic + subscription + IAM grant, mirroring `terraform/modules/quarterly_report/main.tf` exactly**

In `terraform/modules/sanctions_refresh/main.tf`, add (placed the same way `quarterly_report/main.tf` places its own, right after the `archive_file` block and before the execution-role section):

```hcl
# -- staff notification topic ("refresh failed") ----------------------------

resource "aws_sns_topic" "notify" {
  name = "aphrodite-sanctions-refresh-notify"
}

resource "aws_sns_topic_subscription" "emails" {
  for_each  = toset(var.notify_emails)
  topic_arn = aws_sns_topic.notify.arn
  protocol  = "email"
  endpoint  = each.value
}
```

Add a `Notify` statement to the existing `data "aws_iam_policy_document" "exec"` block (alongside `Logs`/`WriteBundle`/`UseKms`):

```hcl
statement {
  sid       = "Notify"
  actions   = ["sns:Publish"]
  resources = [aws_sns_topic.notify.arn]
}
```

Add `APHRODITE_NOTIFY_TOPIC_ARN = aws_sns_topic.notify.arn` to `aws_lambda_function.refresh`'s existing `environment.variables` map.

- [ ] **Step 2: Add the `notify_emails` variable**

In `terraform/modules/sanctions_refresh/variables.tf`, add (mirroring `quarterly_report/variables.tf`'s `notify_emails` exactly):

```hcl
variable "notify_emails" {
  type        = list(string)
  default     = []
  description = "Internal staff emails subscribed to the 'sanctions refresh failed' SNS topic."
}
```

- [ ] **Step 3: Add the `notify_topic_arn` output**

In `terraform/modules/sanctions_refresh/outputs.tf`, add (mirroring `quarterly_report/outputs.tf`'s equivalent output):

```hcl
output "notify_topic_arn" {
  description = "SNS topic ARN for 'sanctions refresh failed' staff notifications."
  value       = aws_sns_topic.notify.arn
}
```

- [ ] **Step 4: Wire the root module**

In `terraform/variables.tf`, add a new root variable immediately after the existing `aml_export_notify_emails` variable (mirroring it exactly):

```hcl
variable "sanctions_refresh_notify_emails" {
  type        = list(string)
  default     = []
  description = "Internal staff emails to notify when the monthly sanctions refresh fails."
}
```

In `terraform/main.tf`, inside the existing `module "sanctions_refresh" { ... }` block, add:

```hcl
  notify_emails = var.sanctions_refresh_notify_emails
```

- [ ] **Step 5: Verify locally**

Run (from `terraform/`, local only — no plan/apply):
```bash
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```
Expected: `fmt` reports no diff; `validate` reports `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add terraform/modules/sanctions_refresh/main.tf terraform/modules/sanctions_refresh/variables.tf terraform/modules/sanctions_refresh/outputs.tf terraform/variables.tf terraform/main.tf
git commit -m "feat: add an SNS alerting topic to the sanctions-refresh Terraform module"
```

---

### Task 5: Sanction Screener — detect and surface bundle staleness (backend)

**Files:**
- Modify: `backend/api/screener.py`
- Test: `backend/screener/tests/test_screener_service.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-4.
- Produces: two new keys in `ScreenerService.screen()`'s returned dict — `bundle_generated_at: str | None` and `bundle_stale: bool` — which Task 6's frontend reads by exact name. Also produces a new pure function `_is_bundle_stale(generated_at: str | None, *, now: datetime, threshold_days: int = 45) -> bool` (module-level, not exported elsewhere, but its own tests pin the staleness-threshold decision this task makes).

**Staleness threshold (this task's own call, per the spec's "What's left to the builder"):** 45 days — the ~30-day monthly refresh cadence plus a 2-week grace window. A missing or unparsable `generated_at` is treated as stale (fail toward warning, never toward silently assuming freshness) — this is a deliberate, conservative choice, consistent with "warn and allow, never block": treating unknown age as stale only adds a banner, never blocks the screening.

- [ ] **Step 1: Write the failing tests for `_is_bundle_stale`**

In `backend/screener/tests/test_screener_service.py`, add (no `ScreenerService`/matcher machinery needed — this is a pure function):

```python
from datetime import UTC, datetime

from backend.api.screener import _is_bundle_stale


def test_fresh_bundle_is_not_stale():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-05-20T00:00:00+00:00", now=now) is False


def test_bundle_past_threshold_is_stale():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-04-01T00:00:00+00:00", now=now) is True  # 61 days


def test_bundle_exactly_at_threshold_boundary():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-04-17T00:00:00+00:00", now=now) is False  # 45 days
    assert _is_bundle_stale("2026-04-16T00:00:00+00:00", now=now) is True  # 46 days


def test_missing_generated_at_is_treated_as_stale():
    assert _is_bundle_stale(None, now=datetime.now(UTC)) is True


def test_timezone_naive_generated_at_does_not_raise():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-05-20T00:00:00", now=now) is False  # no offset
```

Then add a test exercising `screen()` itself:

```python
def test_screen_flags_stale_bundle(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    svc.bundle_generated_at = "2020-01-01T00:00:00+00:00"
    csv = b"name,dob\nClean Person,1990-01-01\n"

    result = svc.screen(csv, "list.csv")

    assert result["bundle_generated_at"] == "2020-01-01T00:00:00+00:00"
    assert result["bundle_stale"] is True


def test_screen_does_not_flag_fresh_bundle(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    svc.bundle_generated_at = datetime.now(UTC).isoformat()
    csv = b"name,dob\nClean Person,1990-01-01\n"

    result = svc.screen(csv, "list.csv")

    assert result["bundle_stale"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/screener/tests/test_screener_service.py -v`
Expected: FAIL — `_is_bundle_stale` doesn't exist; `ScreenerService` has no `bundle_generated_at` field; `screen()`'s return dict has no `bundle_generated_at`/`bundle_stale` keys.

- [ ] **Step 3: Implement**

Add `bundle_generated_at: str | None = None` as a new field on the `ScreenerService` dataclass, placed after the existing `matcher: Any = None` field.

Add the module-level function:

```python
_STALE_BUNDLE_DAYS = 45  # monthly refresh cadence (~30 days) + a 2-week grace window


def _is_bundle_stale(
    generated_at: str | None, *, now: datetime, threshold_days: int = _STALE_BUNDLE_DAYS
) -> bool:
    """True when the bundle is older than threshold_days, or its age is unknown
    (a missing/unparsable generated_at) — unknown age is treated as stale, not fresh."""
    if not generated_at:
        return True
    try:
        generated = datetime.fromisoformat(generated_at)
    except ValueError:
        return True
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    return (now - generated).days > threshold_days
```

In `_get_matcher()`, right after `bundle = self._load_bundle()`, add `self.bundle_generated_at = bundle.get("generated_at")`.

In `screen()`'s `return` dict (inside the `try`, where `build_summary`/`file_hash`/`flagged_results` are already assembled), add:

```python
"bundle_generated_at": self.bundle_generated_at,
"bundle_stale": _is_bundle_stale(self.bundle_generated_at, now=datetime.now(UTC)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/screener/tests/test_screener_service.py -v`
Expected: PASS, all tests (7 pre-existing + 7 new).

- [ ] **Step 5: Commit**

```bash
git add backend/api/screener.py backend/screener/tests/test_screener_service.py
git commit -m "feat: detect and surface sanctions-bundle staleness in screen() results"
```

---

### Task 6: Sanction Screener — stale-bundle banner (frontend)

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/components/ScreenerPanel.tsx`

**Interfaces:**
- Consumes: Task 5's `ScreenResult.bundle_generated_at`/`ScreenResult.bundle_stale` fields, by exact name.
- Produces: nothing consumed by a later task — last task in this plan.

- [ ] **Step 1: Add the two new fields to `ScreenResult`**

In `frontend/src/types.ts`, add to the existing `ScreenResult` interface (after `flagged_results`):

```typescript
bundle_generated_at?: string | null;
bundle_stale?: boolean;
```

- [ ] **Step 2: Add the banner**

In `frontend/src/components/ScreenerPanel.tsx`, inside the existing `{result && (<> ... )}` block, immediately before the `<div className="screen-summary">`, add (reusing this same file's existing `shortDateTime` helper — do not write new date formatting):

```tsx
{result.bundle_stale && (
  <p className="banner banner-warn">
    This screening ran against a sanctions watchlist bundle
    {result.bundle_generated_at
      ? ` generated ${shortDateTime(result.bundle_generated_at)}`
      : " of unknown age"}
    , which may be out of date. The result below is not blocked, but confirm the monthly
    refresh is running if this keeps happening.
  </p>
)}
```

- [ ] **Step 3: Typecheck and build**

Run (in `frontend/`): `npx tsc -b && npm run build`
Expected: both clean, no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types.ts frontend/src/components/ScreenerPanel.tsx
git commit -m "feat: show a non-blocking banner when the sanctions bundle may be stale"
```

---

## Self-Review Notes

- **Spec coverage:** all three Phase 5 items are covered — item 1 (Quarterly/AML pre-loop gap) by Tasks 1-2, item 2 (Sanctions Refresh alerting) by Tasks 3-4, item 3 (stale-bundle banner, warn-never-block) by Tasks 5-6.
- **Type/name consistency checked:** `Deps.notify_failure`'s signature differs intentionally between Tasks 1-2 (`(context: str, error: str)`, matching `notify`'s existing `(period, results, ...)`-style plain-English-message convention) and Task 3 (`(error: str, counts: dict[str, int] | None)`, since Sanctions Refresh has no per-item loop to summarize, only source counts) — this is a deliberate, stated difference, not an inconsistency to fix. `bundle_generated_at`/`bundle_stale` are spelled identically in Task 5 (Python dict keys) and Task 6 (TypeScript field names).
- **Proportion check:** this plan is noticeably longer than the Phase 5 section of the spec it implements, mostly because of fully-spelled-out test code (per this skill's own "exact values" requirement for test steps) across 6 small tasks; each individual task's code change is small (one new `Deps` field + one `except` block per backend task, a handful of Terraform lines, two frontend fields/one banner).
