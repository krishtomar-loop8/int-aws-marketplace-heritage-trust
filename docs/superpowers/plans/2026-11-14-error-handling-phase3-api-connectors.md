# Error-Handling Phase 3: API Catch-All, Connector Fix & Live-Search Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three related backend-robustness fixes, plus making a live Matter
Search failure show the same plain-language translation staff already see
on the Integrations tab, instead of a raw exception string.

**Architecture:** `backend/api/handlers.py`'s synchronous route-dispatch path
and `_run_health_probe()` gain the same catch-all discipline `_run_worker()`
already has. `backend/connectors/graph/connector.py`'s four `"401" in
str(err)` substring checks are replaced with `err.status == 401` (the
`HttpError` exception already carries this). `services/errors.py`'s
`classify()` (Phase 1) is hardened — word-boundary matching for its numeric
HTTP-code markers (today's bare substrings can misfire on digits inside a
request ID or matter number) and connector-agnostic wording for its
`integration=None` path (today it still assumes a connector/health-probe
caller) — both carried forward from Phase 1's final review as required
before any non-connector caller routes through it. `_run_worker()`'s failure
path then calls `classify()` and stores both the resulting message and a new
`error_is_friendly` flag on the job; the frontend's `search()`/`getSearch()`
preserve that flag on the thrown error, and `friendlyError()` trusts an
already-classified message instead of re-judging it by keyword.

**Tech Stack:** Python 3.13 (backend/Lambda), TypeScript/React (frontend).

**Spec:** `docs/superpowers/specs/2026-11-13-product-wide-error-handling-design.md`
(Phase 3 section — items 1-3 — plus the "Coordination across phases" section
describing the `error_is_friendly` field and the health-probe catch-all
extension, plus the shared Goals/Non-goals/Anti-goals/Constraints that bind
every phase).

## Global Constraints

- `suggest_fix(integration: str, detail: str) -> str`'s existing behavior
  (Phase 1's hard constraint) must not regress: it is called only with a
  real `integration` string, never `None`, so none of this phase's
  `integration=None` wording changes may alter any of its existing test
  outcomes. Confirm this explicitly (see Task 3's verification step) rather
  than assuming it.
- The new 500 response from the API catch-all (Task 1) is a **separate**
  mechanism from `error_is_friendly` (Task 4/5) — it is a synchronous route
  error, not a search-job field, and does not carry or need that flag. Do
  not conflate the two.
- `error_is_friendly` defaults to `false` wherever it isn't explicitly set
  to `true` (fail closed on trust — an unclassified message must never be
  shown to staff as if it were already vetted).
- No new frontend test infrastructure — this codebase has none for plain
  TypeScript functions or React components (confirmed: no `*.test.*`/
  `*.spec.*` files, no `test` script in `frontend/package.json`).
  Verification for frontend changes is `tsc -b` + `npm run build` + a real
  dev-server check, matching every prior frontend task in this project.

## Review Focus

- A completely unrecognized exception reaching the API catch-all (Task 1)
  must produce a generic 500 `{"error": ...}` response, not a raw Lambda
  crash — and must not leak internal details (stack trace, file paths) into
  the response body.
- `_run_health_probe()`'s own setup code failing (not a per-connector probe
  failure, which `run_checks()` already isolates) must not crash the
  scheduled invocation silently — it must be caught the same way.
- The Graph connector's 401-retry logic must still correctly retry on a
  **real** 401 after the fix, not just correctly *avoid* retrying on a fake
  one — a test proving the retry still happens is as important as a test
  proving the false positive is gone.
- `classify()`'s hardened digit-matching must not change behavior for any of
  `suggest_fix()`'s existing six tests or Phase 1's twelve `classify()`
  tests — a word-boundary tightening that accidentally stops matching a
  real, correctly-formatted error would be a regression, not a fix.
- A live Matter Search failure must show its classified message end-to-end
  in the actual UI (not just in a unit test) — Task 5's manual dev-server
  verification is not optional polish, it's the only check that proves the
  whole chain (backend classify → job record → frontend fetch → `trust an
  already-classified message`) actually connects.

---

### Task 1: API catch-all — synchronous route dispatch + health probe

**Files:**
- Modify: `backend/api/handlers.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Produces: nothing new consumed by later tasks — this task is independent
  of Tasks 2-5.

- [ ] **Step 1: Write the failing tests**

Add to `backend/api/tests/test_handlers.py` (near the existing
`test_worker_records_failure`/health-probe tests — read a few neighboring
tests first to match fixture/mocking style, e.g. `install_deps`):

```python
def test_unhandled_route_exception_returns_500_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    deps.resolver.resolve = boom  # type: ignore[method-assign]
    out = handlers.handler(api_event("POST", "/resolve", body={"query": "anything"}))
    assert out["statusCode"] == 500
    body = json.loads(out["body"])
    assert "error" in body
    assert "boom" not in body["error"]  # internal exception text is not leaked
```

(`api_event`, `install_deps` are this file's existing fixtures — used
exactly as `test_worker_records_failure` and others already use them.)

```python
def test_health_probe_setup_failure_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.harness as harness_mod

    deps, _ = install_deps(monkeypatch)
    deps.integrations = FakeIntegrations()  # type: ignore[assignment]

    def boom(**_: Any) -> Any:
        raise RuntimeError("secrets manager unreachable")

    monkeypatch.setattr(harness_mod, "run_checks", boom)
    result = handlers.handler({"health_probe": True})
    assert result.get("ok") is False
```

(`FakeIntegrations`, `install_deps`, and the `harness_mod.run_checks`
monkeypatch pattern are the same fixtures the file's existing
`test_health_probe_records_each_source_with_suggested_fix` and
`test_health_probe_only_checks_enabled_connectors` already use — read those
two first to confirm the import/fixture details match exactly.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_handlers.py -k "500_not_a_crash or setup_failure" -v`
Expected: FAIL (today, an unhandled exception in the route dispatch or in
`_run_health_probe`'s own setup propagates as a raised exception, not a
returned dict/500 response)

- [ ] **Step 3: Wrap the synchronous route dispatch**

In `handler()`, after the `deps = _get_deps()` line (the point where the
function commits to the synchronous API-Gateway-request path), wrap the
rest of the function's body (the full `if method == ...` chain through
`return _response(404, {"error": "not found"})`) in a try/except:

```python
try:
    ...  # existing route dispatch, unchanged
except Exception:  # noqa: BLE001 - route dispatch must never crash the Lambda raw
    logger.exception("unhandled exception in route dispatch")
    return _response(500, {"error": "internal error"})
```

Keep the existing `deps = _get_deps()` call, and the `method`/`path`
extraction, **outside** the try block (if `_get_deps()` itself fails, that's
a cold-start/config problem worth a raw crash+CloudWatch alarm, not a
quietly-swallowed 500 — matching the existing precedent that
`_run_worker`'s try/except starts *after* its own `deps = _get_deps()`
line too).

- [ ] **Step 4: Wrap `_run_health_probe()`'s own body**

Wrap everything from `deps.integrations is None` check's `return` onward
(i.e., the `import boto3` / `SecretsClient(...)` / `run_checks(...)` /
result-recording loop) in a try/except that logs and returns
`{"ok": False, "reason": "health probe crashed"}` instead of propagating —
matching the module's own existing discipline that a health-probe failure
must never crash the scheduled invocation. Keep the existing
`if deps.integrations is None: ...` early return outside the new try block
(it's not a failure, it's a deliberate skip).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_handlers.py -v`
Expected: PASS, full file, no regressions.

- [ ] **Step 6: Commit**

```bash
git add backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: add a catch-all to the API route dispatch and health probe"
```

---

### Task 2: Fix the Graph connector's `"401" in str(err)` bug

**Files:**
- Modify: `backend/connectors/graph/connector.py`
- Test: `backend/connectors/graph/tests/test_connector.py`

**Interfaces:**
- Consumes: `services.http.HttpError`'s existing `.status: int | None`
  attribute (already set by the HTTP client wrapper on every raised
  `HttpError` — confirmed via existing `responses`-mocked tests that assert
  on `status=401`).
- Produces: nothing consumed by later tasks — independent of every other
  task in this plan.

- [ ] **Step 1: Write the failing test proving the false positive**

Add to `backend/connectors/graph/tests/test_connector.py` (near the existing
`test_401_triggers_token_refresh_and_retry` — match its fixture style):

```python
@responses.activate
def test_a_403_whose_body_happens_to_mention_401_does_not_trigger_a_refresh() -> None:
    # A real error unrelated to auth, whose message text happens to contain the
    # digits "401" (e.g. a request id) — must NOT be treated as a 401.
    _add_token(token="tok-1")
    responses.add(
        responses.GET,
        f"{BASE}/users/u1",
        json={"error": {"code": "Forbidden", "requestId": "a401b-c-d"}},
        status=403,
    )
    connector, _ = _make_connector()
    with pytest.raises(HttpError) as exc_info:
        connector.get_user("u1")
    assert exc_info.value.status == 403
    # Only the token fetch plus one data call were made — no 401-retry/refresh.
    data_calls = [c for c in responses.calls if c.request.url == f"{BASE}/users/u1"]
    assert len(data_calls) == 1
```

(`_add_token`, `_make_connector`, `BASE` are this file's existing fixtures —
used exactly as `test_401_triggers_token_refresh_and_retry` already uses
them, just asserting the opposite outcome. Write one such test for each of
the four call sites — `_get_json` i.e. `get_user`, `_paginate`,
`iter_drive_delta`, `download_drive_item` — following each existing
401-retry test's own fixture pattern for that method. Four new tests total,
one per site.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/connectors/graph/tests/test_connector.py -k "does_not_trigger" -v`
Expected: FAIL (today's `"401" in str(err)` substring check matches the
"a401b-c-d" request id text, incorrectly triggering a refresh+retry, so
`len(responses.calls)` would be 2, not 1)

- [ ] **Step 3: Fix the four call sites**

In `backend/connectors/graph/connector.py`, replace each of the four
`if "401" in str(err):` (and the one `if first and "401" in str(err):`)
checks with `if err.status == 401:` (and `if first and err.status == 401:`
for the one with the `first` guard) — `err` is already the caught
`HttpError` in every one of these `except HttpError as err:` blocks, and
`HttpError.status` is already populated by the HTTP client. This matches
the pattern the connector's own `search()` method already uses correctly
(`response.status_code == 401`, on a raw response object in a different
code path — the fix here uses the exception's own `.status` instead, since
these four sites catch `HttpError`, not a raw response).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/connectors/graph/tests/test_connector.py -v`
Expected: PASS, full file, no regressions — including the pre-existing
`test_401_triggers_token_refresh_and_retry` and
`test_401_after_reconnect_uses_fresh_secret_not_cache`, proving the fix
still correctly retries on a **real** 401, not just correctly ignores a
fake one.

- [ ] **Step 5: Commit**

```bash
git add backend/connectors/graph/connector.py backend/connectors/graph/tests/test_connector.py
git commit -m "fix: Graph connector checks the real HTTP status, not a substring, for 401"
```

---

### Task 3: Harden `classify()` — word-boundary matching + connector-agnostic wording

**Files:**
- Modify: `services/errors.py`
- Test: `services/tests/test_errors.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: the same `classify(detail: str, *, integration: str | None =
  None) -> ClassifiedError` signature, unchanged — only its internal
  matching and its `integration=None` message text change. Task 4 depends
  on this task landing first (it calls `classify()` with `integration=None`
  for the first time in production code).

This is Phase 1's final-review debt, now due: that review found (a) the
numeric HTTP-code markers (`"402"`, `"403"`, `"429"`, `"500"`, `"502"`,
`"503"`, `"504"`, and the `" 401"`/`":401"` auth markers) are bare substring
checks that can misfire on digits inside a request ID or matter number, and
(b) every `integration=None` message still assumes a connector/health-probe
caller (e.g. "check the source system's status page"), which is wrong
advice once a non-connector caller (this phase's live Matter Search path)
routes through it.

- [ ] **Step 1: Write the failing tests**

Add to `services/tests/test_errors.py`:

```python
def test_word_boundary_prevents_request_id_false_positive() -> None:
    # "403" appears inside a request id, not as the actual HTTP status.
    result = classify("ValidationException: bad input (RequestId: a403f-01-02)")
    assert result.error_class != ErrorClass.PERMANENT

def test_word_boundary_prevents_matter_number_false_positive() -> None:
    result = classify("ValueError: matter 1500 not found")
    assert result.error_class != ErrorClass.TRANSIENT

def test_word_boundary_still_matches_a_real_403() -> None:
    result = classify("HttpError: 403 Forbidden")
    assert result.error_class == ErrorClass.PERMANENT

def test_word_boundary_still_matches_a_real_401_auth_marker() -> None:
    result = classify("HttpError: 401 Unauthorized")
    assert result.error_class == ErrorClass.NEEDS_REAUTH

def test_generic_message_never_assumes_a_source_system_or_secrets_manager() -> None:
    for detail in (
        "HttpError: 402 Payment Required",
        "HttpError: 403 Forbidden",
        "HttpError: 429 Too Many Requests",
        "ReadTimeout: read timed out",
        "HttpError: 503 ServiceUnavailable",
        "ValueError: something odd",
    ):
        msg = classify(detail).message.lower()
        assert "source system" not in msg
        assert "secrets manager" not in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest services/tests/test_errors.py -k "word_boundary or generic_message_never" -v`
Expected: FAIL

- [ ] **Step 3: Tighten the digit markers to word boundaries**

Add `import re` to `services/errors.py`. Replace each bare substring check
on a numeric code (`"402" in d`, `"403" in d`, `"429" in d`, the
`"500"`/`"502"`/`"503"`/`"504"` tuple membership, and the `" 401"`/`":401"`
entries in `auth_markers`) with a word-boundary regex check, e.g.
`re.search(r"\b402\b", d)` in place of `"402" in d`, and
`re.search(r"\b401\b", d)` in place of the `" 401"`/`":401"` markers (drop
those two string markers from `auth_markers` entirely once the regex check
covers the same case more precisely). Leave the non-numeric keyword markers
(`"token refresh failed"`, `"invalid_grant"`, `"unauthorized"`,
`"throttl"`, `"rate limit"`, `"too many requests"`, `"timed out"`,
`"timeout"`, `"readtimeout"`, `"connecttimeout"`, `"connection"`,
`"serviceunavailable"`, `"server error"`, `"forbidden"`) as plain substring
checks — these are lower false-positive-risk than bare digits and the
Phase 1 final review only flagged the numeric codes.

- [ ] **Step 4: Neutralize the `integration=None` wording**

Replace every `integration=None`-branch message's "the source system"/"The
source system"/"source-side" framing with neutral, caller-agnostic
phrasing that doesn't claim *where* the failure happened — use "the
request"/"the service" as the only two referents (e.g. "A billing/payment
hold was reported (HTTP 402). Check the account's subscription/billing
status." instead of "The source system reported a billing/payment hold on
the account..."; "An unexpected error occurred. If it persists, check the
service's status and the request's configuration." instead of "...check the
credentials in Secrets Manager and the source system's status page." for
the UNKNOWN/default branch — dropping the Secrets-Manager assumption
entirely for the generic path, since that's only true for a connector
caller). Do not change any `integration=<name>` (named) branch's wording —
those are Phase 1's byte-for-byte-verified hard constraint for
`suggest_fix()` and must not move.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest services/tests/test_errors.py -v`
Expected: PASS, all tests (the 12 from Phase 1 plus this task's new ones).

- [ ] **Step 6: Confirm `suggest_fix()`'s hard constraint is untouched**

Run: `python -m pytest backend/api/tests/test_integrations.py -v`
Expected: PASS, all 16 tests, unedited — proving the named-path wording and
matching behavior `suggest_fix()` depends on is unaffected by this task's
changes (which only touch the digit-matching logic shared by both paths
and the unnamed-path wording).

- [ ] **Step 7: Commit**

```bash
git add services/errors.py services/tests/test_errors.py
git commit -m "fix: word-boundary match classify()'s digit markers, neutralize generic wording"
```

---

### Task 4: Route live Matter Search failures through `classify()`

**Files:**
- Modify: `backend/api/handlers.py` (`_run_worker`)
- Modify: `backend/api/jobs.py` (`JobStore` Protocol, `InMemoryJobStore`,
  `DynamoDbJobStore`)
- Modify: `backend/api/tests/test_handlers.py` (update one existing test)
- Test: `backend/api/tests/test_jobs.py`

**Interfaces:**
- Consumes: Task 3's `classify(detail, *, integration=None) -> ClassifiedError`
  (`.message: str`).
- Produces: `JobStore.fail(self, job_id: str, error: str, *,
  error_is_friendly: bool = False) -> None` (new keyword-only parameter,
  default `False`); `JobStore.get()`'s returned dict gains an
  `error_is_friendly: bool` key whenever the job's `error` was set with that
  flag `True` (omitted/defaults-false otherwise). Task 5 consumes this exact
  key name from the job-status JSON the frontend already polls.

- [ ] **Step 1: Write the failing `JobStore` tests**

Add to `backend/api/tests/test_jobs.py` (near the existing
`test_inmemory_fail`/`test_dynamo_fail_records_error`):

```python
def test_inmemory_fail_records_error_is_friendly_flag() -> None:
    store = InMemoryJobStore(clock=_clock)
    store.create("j1", "q", None)
    store.fail("j1", "A classified message", error_is_friendly=True)
    assert store.get("j1")["error_is_friendly"] is True

def test_inmemory_fail_defaults_error_is_friendly_to_false() -> None:
    store = InMemoryJobStore(clock=_clock)
    store.create("j1", "q", None)
    store.fail("j1", "raw text")
    assert store.get("j1").get("error_is_friendly") is not True

def test_dynamo_fail_records_error_is_friendly_flag() -> None:
    fake = FakeDynamo()
    store = DynamoDbJobStore("jobs", client=fake, clock=_clock)
    store.create("j1", "q", None)
    store.fail("j1", "A classified message", error_is_friendly=True)
    assert store.get("j1")["error_is_friendly"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_jobs.py -k error_is_friendly -v`
Expected: FAIL (`TypeError: fail() got an unexpected keyword argument`)

- [ ] **Step 3: Extend `JobStore` Protocol and `InMemoryJobStore`**

In `backend/api/jobs.py`:
- `JobStore` Protocol: `def fail(self, job_id: str, error: str, *, error_is_friendly: bool = False) -> None: ...`
- `InMemoryJobStore.fail`: add the same parameter, and `item.update(status=STATUS_ERROR, error=error, error_is_friendly=error_is_friendly, updated_at=self._clock())`.

- [ ] **Step 4: Extend `DynamoDbJobStore`**

`DynamoDbJobStore._update()` currently takes one `(field, value)` pair.
Generalize its signature to `_update(self, job_id: str, status: str,
fields: dict[str, dict[str, Any]]) -> None`, building the
`UpdateExpression`/`ExpressionAttributeNames`/`ExpressionAttributeValues`
from the `fields` dict instead of a single field (it has exactly one
existing caller, `fail()`, so this is a safe, fully-covered change — grep
confirms no other caller). Update `fail()`:

```python
def fail(self, job_id: str, error: str, *, error_is_friendly: bool = False) -> None:
    self._update(
        job_id,
        STATUS_ERROR,
        {"error": {"S": error[:1024]}, "error_is_friendly": {"BOOL": error_is_friendly}},
    )
```

In `get()`, add projection for the new attribute alongside the existing
`if "error" in item:` block: `if "error_is_friendly" in item: out["error_is_friendly"] = item["error_is_friendly"]["BOOL"]`.

- [ ] **Step 5: Run the `JobStore` tests**

Run: `python -m pytest backend/api/tests/test_jobs.py -v`
Expected: PASS, full file, no regressions.

- [ ] **Step 6: Wire `_run_worker`'s failure path through `classify()`**

In `backend/api/handlers.py`'s `_run_worker`, replace the except block's
body:

```python
except Exception as exc:  # noqa: BLE001 - record failure on the job, don't crash the worker
    logger.exception("search worker failed", extra={"extra_fields": {"job_id": job_id}})
    classified = classify(f"{type(exc).__name__}: {exc}")
    deps.store.fail(job_id, classified.message, error_is_friendly=True)
    return {"ok": False, "job_id": job_id}
```

`f"{type(exc).__name__}: {exc}"`, not bare `str(exc)` — this matches
`backend/harness.py`'s own existing convention for building a `detail`
string (`detail = f"{type(exc).__name__}: {exc}"`, confirmed by reading it),
which is the same `"<ExcType>: <message>"` shape `classify()`/`suggest_fix()`
already pattern-match against. `str(exc)` alone drops the exception type
name (e.g. `str(RuntimeError("x"))` is just `"x"`, not `"RuntimeError: x"`),
which would silently weaken classification accuracy for exactly the
exceptions most likely to carry a useful type name (an `HttpError` bubbling
up from a connector, for instance).

Import `classify` from `services.errors` at the top of the file (next to
the existing `suggest_fix` import). `error_is_friendly=True` is
unconditional here — `classify()` always returns a message (its `UNKNOWN`
fallback is itself a catalog-produced, staff-ready message, never a raw
exception string), so every failure this path records is, by construction,
already classified.

- [ ] **Step 7: Update the one existing test this change affects**

`backend/api/tests/test_handlers.py`'s `test_worker_records_failure`
currently asserts `"exploded" in deps.store.get("j1")["error"]` — the raw
exception text. After this change, the stored message is `classify()`'s
catalog message (for an unrecognized `RuntimeError`, its `UNKNOWN`-branch
text), which does not contain "exploded". Update the assertion to match the
new, intended behavior:

```python
def test_worker_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("resolve exploded")

    deps.resolver.resolve = boom  # type: ignore[method-assign]
    deps.store.create("j1", "q", {"sub": "u"})
    out = handlers.handler({"worker": True, "job_id": "j1", "query": "q", "actor": None})
    assert out["ok"] is False
    job = deps.store.get("j1")
    assert job["status"] == "error"
    assert job["error_is_friendly"] is True
    assert "exploded" not in job["error"]  # raw exception text no longer shown
```

This is an intentional, expected edit to an existing test (not a
regression) — the whole point of this task is that the raw exception text
no longer reaches the job record.

- [ ] **Step 8: Run the full backend test suite**

Run: `python -m pytest backend/ services/ -q`
Expected: PASS, no regressions anywhere else.

- [ ] **Step 9: Commit**

```bash
git add backend/api/handlers.py backend/api/jobs.py backend/api/tests/test_handlers.py backend/api/tests/test_jobs.py
git commit -m "feat: classify a live search-job failure instead of storing the raw exception"
```

---

### Task 5: Frontend — trust an already-classified job error

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/components/MatterSearchChat.tsx`

**Interfaces:**
- Consumes: Task 4's `error_is_friendly` key on the polled job JSON
  (`GET /search/{id}`'s response body, already fetched as `job` in
  `search()`/`getSearch()`).
- Produces: nothing consumed by a later task — this is the last task in
  this plan.

- [ ] **Step 1: Add a typed job-error carrier**

In `frontend/src/api.ts`, add:

```typescript
export class SearchJobError extends Error {
  isFriendly: boolean;
  constructor(message: string, isFriendly: boolean) {
    super(message);
    this.isFriendly = isFriendly;
  }
}
```

- [ ] **Step 2: Throw it from the two job-status-error sites**

In `search()`, replace `if (job.status === "error") throw new
Error(job.error || "Search job failed");` with
`if (job.status === "error") throw new SearchJobError(job.error || "Search job failed", Boolean(job.error_is_friendly));`.
Do the same in `getSearch()` for its equivalent line. Leave every other
`throw new Error(...)` in this file (the HTTP-status-based ones — `Search
failed (${status})`, `Status check failed (${status})`, the timeout) as
plain `Error`s, unchanged — those are raw transport failures, never
catalog-classified, and must still go through `friendlyError()`'s existing
keyword matching.

- [ ] **Step 3: Make `friendlyError()` trust an already-classified message**

```typescript
export function friendlyError(raw?: string, isFriendly?: boolean): string {
  if (isFriendly && raw) return raw;
  const s = (raw || "").toLowerCase();
  // ...unchanged keyword-matching body below...
}
```

- [ ] **Step 4: Update the one call site**

In `frontend/src/components/MatterSearchChat.tsx`'s catch block, replace
`const msg = friendlyError(e instanceof Error ? e.message : "");` with:

```typescript
const isFriendly = e instanceof SearchJobError && e.isFriendly;
const msg = friendlyError(e instanceof Error ? e.message : "", isFriendly);
```

Add `SearchJobError` to this file's existing `import { ... } from "../api"`
line.

- [ ] **Step 5: Typecheck and build**

Run: `npx tsc -b` and `npm run build` (in `frontend/`).
Expected: both clean.

- [ ] **Step 6: Manually verify in the dev server (Review Focus)**

Start the dev server, run a Matter Search that deliberately fails in a way
that reaches `_run_worker`'s except block (e.g., temporarily make
`deps.resolver`/the mock path raise, or use mock mode's existing error-
simulation path if one exists — check `frontend/src/mock.ts` first). Confirm
the chat shows the classify()-produced message verbatim (not the old
generic "Something went wrong running this search" line), proving the whole
chain connects: backend classifies → job record carries
`error_is_friendly: true` → frontend trusts it → UI shows the specific
message. Then confirm a genuine transport-level failure (e.g., temporarily
point `apiBase` at an unreachable URL) still shows one of `friendlyError()`'s
existing keyword-matched lines, proving that path is unregressed. Revert
any temporary changes before committing.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api.ts frontend/src/components/MatterSearchChat.tsx
git commit -m "feat: show a classified search failure verbatim instead of re-genericizing it"
```
