# Error-Handling Phase 4: Bedrock/LLM Runtime Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runtime Bedrock failure during a live Matter Search gives the
same specific, actionable message the installer's Bedrock pre-flight check
already gives at deploy time (e.g. "enable this model in the Bedrock
console"), instead of today's generic "please try again" — and a partial
answer (valid JSON, but the model omitted some expected sections) tells
staff which sections are missing instead of silently rendering as if it
were complete.

**Architecture:** Phase 3 already wired every `_run_worker` failure —
including a Bedrock `LLMError` — through `services/errors.py`'s
`classify()`, with the result trusted end-to-end by the frontend via
`error_is_friendly`. No new backend wiring is needed: this phase only adds
a Bedrock-aware branch to `classify()` itself (gated on a "bedrock" marker
so it can't misfire for a non-Bedrock caller), checked before the generic
HTTP-code branches so a Bedrock error code like `ThrottlingException`
doesn't fall through to a message that cites an HTTP status that never
appeared. Once this branch exists, every Bedrock failure — including the
ones that land in `ErrorClass.UNKNOWN` — gets Bedrock-appropriate wording,
which resolves the question Phase 3's final review left open ("does
Phase 4 need an `error_is_friendly`-bypass fallback?"): it doesn't — the
frontend's old AI-specific `friendlyError()` keyword branches become dead
code and are removed. Separately, `frontend/src/components/StatusReport.tsx`
reads the already-computed, already-typed `missing_sections` field (unused
since an earlier phase) and renders a banner naming the missing sections.

**Tech Stack:** Python 3.13 (backend), TypeScript/React (frontend).

**Spec:** `docs/superpowers/specs/2026-11-13-product-wide-error-handling-design.md`
(Phase 4 section, including the "Carried forward from Phase 3's final
review" subsection with the exact design constraints this plan must
satisfy, plus the shared Goals/Non-goals/Anti-goals/Constraints binding
every phase).

## Global Constraints

- The Bedrock-aware branch must be **gated on a "bedrock" marker** in the
  lowercased detail string (e.g. `"bedrock" in d` or `"llmerror" in d`) —
  it must never fire for a non-Bedrock caller's text, and it must be
  checked **before** the existing 429/timeout/5xx branches (per Phase 3's
  final review: a Bedrock `ThrottlingException` today incorrectly produces
  a message citing "HTTP 429", a status code that never appeared in the
  original error).
- `backend/api/tests/test_integrations.py`'s 16 tests (Phase 1's hard
  constraint) must keep passing **unedited** — none of `suggest_fix()`'s
  existing test inputs contain a "bedrock" marker, so the new branch must
  not be reachable from any of them; confirm this explicitly rather than
  assuming it.
- `services/llm.py`'s exact `LLMError` message shapes (do not invent
  others — these are the only four a live search can actually produce):
  `f"Unexpected Bedrock Converse response shape: {exc}"`,
  `"Bedrock Converse timed out waiting for the response"`,
  `f"Bedrock Converse failed ({code or 'unknown'})"` (where `code` is a
  Bedrock `ClientError`'s error code — `AccessDeniedException`,
  `ValidationException`, or one of the retryable codes after retries are
  exhausted: `ThrottlingException`, `ServiceUnavailableException`,
  `ModelTimeoutException`, `InternalServerException`,
  `ModelNotReadyException`), and
  `f"Bedrock connection failed ({type(exc).__name__})"`. `_run_worker`
  wraps these as `f"LLMError: {message}"` before calling `classify()`
  (confirmed: `backend/api/handlers.py`'s `_run_worker` already does
  `classify(f"{type(exc).__name__}: {exc}")` for every exception, unmodified
  by this plan).
- No new frontend test infrastructure — this codebase has none.
  Verification for the frontend task is `tsc -b` + `npm run build` + a real
  dev-server check, matching every prior frontend task in this project.

## Review Focus

- A Bedrock error whose code is `AccessDeniedException` (the real-world
  "model not enabled in this region" case) must produce a message naming
  the Bedrock console and the model-access action — not a generic
  credentials/status-page message, and not the connector-flavored UNKNOWN
  default.
- A Bedrock error whose code is one of the known retryable codes (meaning
  Bedrock's own retry budget was already exhausted inside
  `_converse_with_retries` before raising) must classify as `TRANSIENT`
  with wording that says "the AI service," never an HTTP status code that
  was never actually present in the original error.
- The same message a connector-named caller already gets (e.g.
  `suggest_fix()`'s existing output) must not change at all — the new
  Bedrock branch is additive, checked only for `integration=None` callers
  in practice (today, only `_run_worker` calls `classify()` with
  `integration=None`), and must not alter the priority-order outcome for
  any existing connector-shaped input.
- `missing_sections` being non-empty must never also trigger
  `generationFailed`'s error banner — a partial-but-parsed answer is a
  different, milder case than a fully failed generation, and both banners
  must be able to coexist with `insufficient_info`'s existing banner
  without one clobbering the other.
- After removing `friendlyError()`'s now-dead AI-specific branches, the
  function's remaining behavior (the timeout/throttle keyword branches,
  the generic fallback) must be provably unchanged for the one case that
  can still reach it — a genuine transport-level failure (e.g. `Search
  failed (500)`), which was never AI-specific text in the first place.

---

### Task 1: Bedrock-aware classification in `classify()`

**Files:**
- Modify: `services/errors.py`
- Test: `services/tests/test_errors.py`

**Interfaces:**
- Consumes: nothing new — same `classify(detail: str, *, integration: str
  | None = None) -> ClassifiedError` signature from Phase 1/3.
- Produces: no new public interface — `classify()`'s behavior is extended,
  not its shape. Task 2 does not depend on this task's internals (it only
  renders `missing_sections`, unrelated to `classify()`), so these two
  tasks are independent and can be reviewed in either order.

- [ ] **Step 1: Write the failing tests**

```python
# services/tests/test_errors.py

def test_bedrock_access_denied_names_the_bedrock_console() -> None:
    result = classify("LLMError: Bedrock Converse failed (AccessDeniedException)")
    assert result.error_class == ErrorClass.PERMANENT
    assert "bedrock console" in result.message.lower()
    assert "model access" in result.message.lower()

def test_bedrock_throttling_is_transient_and_never_cites_http_429() -> None:
    result = classify("LLMError: Bedrock Converse failed (ThrottlingException)")
    assert result.error_class == ErrorClass.TRANSIENT
    assert "429" not in result.message
    assert "ai" in result.message.lower() or "model" in result.message.lower()

def test_bedrock_service_unavailable_is_transient() -> None:
    result = classify("LLMError: Bedrock Converse failed (ServiceUnavailableException)")
    assert result.error_class == ErrorClass.TRANSIENT

def test_bedrock_timeout_is_transient() -> None:
    result = classify("LLMError: Bedrock Converse timed out waiting for the response")
    assert result.error_class == ErrorClass.TRANSIENT

def test_bedrock_connection_failure_is_transient() -> None:
    result = classify("LLMError: Bedrock connection failed (ReadTimeoutError)")
    assert result.error_class == ErrorClass.TRANSIENT

def test_bedrock_unrecognized_code_is_bedrock_flavored_unknown() -> None:
    result = classify("LLMError: Bedrock Converse failed (ValidationException)")
    assert result.error_class == ErrorClass.UNKNOWN
    assert "connector" not in result.message.lower()  # not the connector-flavored default
    assert "ai" in result.message.lower() or "model" in result.message.lower()

def test_bedrock_response_shape_error_is_bedrock_flavored_unknown() -> None:
    result = classify("LLMError: Unexpected Bedrock Converse response shape: KeyError('output')")
    assert result.error_class == ErrorClass.UNKNOWN

def test_bedrock_branch_never_fires_for_a_named_connector_caller() -> None:
    # Sanity: a Bedrock marker in a connector-named call should not exist in
    # practice, but confirm the branch is keyed off the detail text, not the
    # integration argument, and doesn't accidentally change named output when
    # no Bedrock marker is present.
    result = classify("HttpError: 403 Forbidden", integration="clio")
    assert result.error_class == ErrorClass.PERMANENT
    assert "bedrock" not in result.message.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest services/tests/test_errors.py -k bedrock -v`
Expected: FAIL (no Bedrock-aware branch exists yet; these inputs currently
fall through to the generic 429/timeout/UNKNOWN branches with the wrong
wording — confirm this is what actually happens, not just an import error)

- [ ] **Step 3: Add the Bedrock-aware branch**

Insert a new branch in `classify()`, checked **first** (before the auth
marker check, since a Bedrock marker should never be mistaken for a
connector auth failure either) — guard the whole branch on
`"bedrock" in d`:

```python
if "bedrock" in d:
    if "accessdeniedexception" in d:
        return ClassifiedError(
            ErrorClass.PERMANENT,
            "This deployment's Bedrock model isn't enabled in this region. Enable it in the "
            "Bedrock console → Model access, then try the search again.",
        )
    retryable_exhausted = (
        "throttlingexception",
        "serviceunavailableexception",
        "modeltimeoutexception",
        "internalserverexception",
        "modelnotreadyexception",
        "timed out",
        "connection failed",
    )
    if any(k in d for k in retryable_exhausted):
        return ClassifiedError(
            ErrorClass.TRANSIENT,
            "The AI service is busy or temporarily unreachable. This is usually transient — "
            "please try again shortly.",
        )
    return ClassifiedError(
        ErrorClass.UNKNOWN,
        "The AI service had a problem generating this summary. If it persists, check the "
        "Bedrock model's status and this deployment's configuration.",
    )
```

Match `"internalservererror"` exactly against the lowercased detail text —
confirm the real Bedrock error code's casing survives lowercasing
correctly (it will, since `classify()` already lowercases `detail` via
`d = (detail or "").lower()`). Double-check the retryable-exhausted keyword
list against `services/llm.py`'s `_RETRYABLE_ERRORS` frozenset (5 codes) —
list them all, don't abbreviate.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest services/tests/test_errors.py -v`
Expected: PASS, all tests (the existing ones plus this task's 8 new ones).

- [ ] **Step 5: Confirm the hard constraint is untouched**

Run: `python -m pytest backend/api/tests/test_integrations.py -v`
Expected: PASS, all 16 tests, unedited — none of `suggest_fix()`'s existing
test inputs contain "bedrock", so this new branch must not change any of
their outcomes.

- [ ] **Step 6: Commit**

```bash
git add services/errors.py services/tests/test_errors.py
git commit -m "feat: classify Bedrock/LLM failures with model-access-aware messages"
```

---

### Task 2: Frontend — missing-sections banner + dead-code cleanup

**Files:**
- Modify: `frontend/src/components/StatusReport.tsx`
- Modify: `frontend/src/api.ts`

**Interfaces:**
- Consumes: `Answer.missing_sections: string[]` (already typed in
  `frontend/src/types.ts`, already populated by the backend — nothing to
  add on that side) and `SECTION_ORDER: { key: string; title: string }[]`
  (already exported from `frontend/src/types.ts`) to map each missing key
  to its human-readable title.
- Produces: nothing consumed by a later task — this plan has 2 independent
  tasks, both last.

- [ ] **Step 1: Add the missing-sections banner**

In `StatusReport.tsx`, inside the `!generationFailed` branch (where
`insufficient_info`'s existing banner already lives), add a sibling
condition so both banners can render together when both are true. Replace:

```tsx
) : (
  answer.insufficient_info && (
    <p className="banner banner-warn">
      Some sources were unavailable for this search, so parts of the picture may be missing
      — treat unconfirmed items as outstanding.
      {answer.insufficient_reasons?.[0] ? ` (${answer.insufficient_reasons[0]})` : ""}
    </p>
  )
)}
```

with:

```tsx
) : (
  <>
    {answer.insufficient_info && (
      <p className="banner banner-warn">
        Some sources were unavailable for this search, so parts of the picture may be missing
        — treat unconfirmed items as outstanding.
        {answer.insufficient_reasons?.[0] ? ` (${answer.insufficient_reasons[0]})` : ""}
      </p>
    )}
    {answer.missing_sections.length > 0 && (
      <p className="banner banner-warn">
        This summary is missing:{" "}
        {answer.missing_sections
          .map((key) => SECTION_ORDER.find((s) => s.key === key)?.title ?? key)
          .join(", ")}
        . The model's reply may have been incomplete — run the search again if you need the
        full picture.
      </p>
    )}
  </>
)}
```

- [ ] **Step 2: Remove `friendlyError()`'s now-dead AI-specific branches**

In `frontend/src/api.ts`, `friendlyError()` currently has:

```typescript
if (
  s.includes("throttl") ||
  s.includes("overload") ||
  s.includes("serviceunavailable") ||
  s.includes("too many requests") ||
  s.includes("connection failed")
)
  return "The AI service was busy. Please try again in a moment.";
if (s.includes("converse failed") || s.includes("bedrock"))
  return "The AI service had a problem generating this summary. Please try again.";
```

These two branches exist to catch raw `LLMError` text that used to reach
`friendlyError()` unclassified. After Task 1, every Bedrock failure is
classified by `classify()` and reaches the frontend with
`error_is_friendly: true` (set unconditionally by `_run_worker`, from
Phase 3) — meaning `friendlyError()`'s `if (isFriendly && raw) return raw;`
short-circuit (added in Phase 3) always fires first for a job-level
Bedrock error, and these two keyword branches can never be reached by any
live code path. Delete both branches, leaving the timeout branch, the
generic fallback, and the new isFriendly short-circuit. Do not remove or
alter the isFriendly short-circuit itself, the timeout branch, or the
final generic fallback line — only these two now-dead branches.

Before deleting, grep `frontend/src` for any other possible path into
`friendlyError()` that isn't `MatterSearchChat.tsx`'s one call site
(confirmed in Phase 3's review to be the only caller) — if you find none,
proceed; if you find one that could still pass raw Bedrock text with
`isFriendly` false or undefined, stop and report back (`NEEDS_CONTEXT`)
rather than deleting.

- [ ] **Step 3: Typecheck and build**

Run: `npx tsc -b` and `npm run build` (in `frontend/`).
Expected: both clean.

- [ ] **Step 4: Manually verify in the dev server (Review Focus)**

`frontend/src/mock.ts`'s `MOCK_ANSWER`/`MOCK_RESULT` fixtures set
`missing_sections: []` — temporarily set a non-empty array (e.g.
`["risks_blockers", "distribution_readiness"]`) on one of them, confirm
the new banner renders with the correct human-readable section titles
(not raw keys), confirm it coexists correctly with the
`insufficient_info` banner when both are set on the same fixture, and
confirm `generationFailed`'s error banner still takes priority and
suppresses both when `parse_ok` is explicitly set `false` on the same
fixture. Revert the temporary fixture change before committing — `git
diff` must be clean except this task's two real files.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/StatusReport.tsx frontend/src/api.ts
git commit -m "feat: show which sections a partial answer is missing, retire dead AI-error copy"
```
