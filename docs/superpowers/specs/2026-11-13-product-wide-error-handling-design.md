# Product-wide error handling: classify, surface, and alert consistently

Source: GitHub Issue #3. Recon (4 parallel surveys + manual review) was completed before this
document; this document turns that recon into a sized, sequenced, buildable plan.

## Intent and the why

The same pattern repeats across the product: a good, specific, actionable error message
already exists somewhere in the codebase for a given failure, but it doesn't reach every place
that failure can actually surface. Staff and customers end up seeing a generic "something
failed" message instead of "your QuickBooks token expired, reconnect it here" — even though the
product already knows how to say the specific thing in at least one other code path.

This work closes that gap everywhere it currently exists, without introducing a new generic
error-handling framework. All 8 originally-identified gaps are judged equally important — there
is no single fire being put out, so this is systematic cleanup, not an urgent fix.

## Goals

- Every place a classifiable failure can surface (deploy time, connector calls, LLM synthesis,
  scheduled jobs, the API layer, the frontend) gives staff/the customer a specific cause and a
  concrete next action, not a generic message, wherever the codebase already knows how to be
  specific.
- Scheduled/unattended jobs (Quarterly Reports, AML Export, Sanctions Refresh) never fail
  silently — every failure path reaches the job's own alerting mechanism.
- The Sanction Screener never silently screens against stale watchlist data — staff are told
  when the bundle is stale, but are not blocked from completing a screening (see Constraints).
- Fix the one confirmed correctness bug found during recon: the Graph connector's `"401" in
  str(err)` substring checks (4 call sites, `backend/connectors/graph/connector.py`), which can
  misfire on an unrelated error that happens to contain the digits "401".

## Non-goals

- No new generic, pluggable "error framework" with registries, middleware chains, or abstract
  base classes. The existing style — a deterministic pattern-match function returning a
  plain-language string, like `suggest_fix()` in `backend/api/integrations.py` — is the model to
  extend, not replace.
- No change to the existing audit trail (CloudTrail/CloudWatch, the activity log). This work is
  about user/staff-facing messages and alerting, not about what gets recorded for compliance
  audit.
- No redesign of the frontend's error-banner visual language (`banner-error`/`banner-warn`
  classes already exist and are reused, not reinvented).
- Not retrying or masking failures that are genuinely permanent (a mis-configured secret, a
  disabled Bedrock model) — the goal is a better message about the same failure, not making the
  failure disappear.

## Anti-goals (would make this a failure even if it "works")

- A single shared Python module imported by both `backend/` and the installer's Lambda handler.
  The installer's handler is deliberately self-contained (stdlib + boto3/botocore only, inlined
  into the CloudFormation template as `index.py`) and cannot gain a dependency on `services/`.
  Phases that touch both sides (Phase 1 + Phase 2) must produce a shared *taxonomy and message
  style*, implemented twice where the two codebases can't share code — not literally shared code.
- Breaking the existing `Answer`/`SynthesisResult` shape or the API response contract the
  frontend already depends on (`parse_ok`, `missing_sections`, etc. already exist end-to-end —
  see Phase 4's "already there" note below). Extending these is fine; renaming or restructuring
  them is not, since nothing about their current shape is wrong.
- Scope creep into infrastructure that doesn't exist yet for its own sake (e.g. standing up a new
  SNS topic for Sanctions Refresh is in scope because it has *no* alerting path today; adding a
  second notification channel to a job that already alerts is not).

## Constraints

- **Sanction Screener stale-bundle behavior: warn and allow, never block.** Decided explicitly —
  this is a compliance-adjacent product decision, not an engineering default. Staff must be told
  clearly when a screening ran against a possibly-stale bundle (banner + the bundle's generated
  date), but must never be prevented from completing a search because the bundle is stale.
- **Installer code stays self-contained.** See the anti-goal above — no new imports from
  `services/` into `installer/function/handler.py` or the inlined CFN Lambda.
- Every phase ships as its own branch and PR, reviewed the same way the deployment-config and
  first-run-setup-wizard features were (subagent-driven development: per-task review, a final
  whole-branch review, one fix wave, one scoped re-review).
- Phases run back-to-back without pausing for sign-off between them — status updates after each
  phase completes, matching this project's existing standing instruction, but no approval gate
  in between. (The approval gate for *this document* is the one that matters; once it's
  approved, the six phases proceed as a continuous sequence.)

## Coordination across phases (resolved during builder-check)

A fresh-eyes builder-check on this document, done before handing it over, surfaced one real
cross-phase gap and one scope-consistency question. Both are resolved here (engineering calls,
not product-policy calls — see each phase's note below for where this changes scope).

**The `friendlyError()` gap.** Phase 3 item 3 and Phase 4 item 1 both route a live Matter
Search's error through the new catalog so it carries a specific, plain-language message instead
of a raw exception string. But `frontend/src/components/MatterSearchChat.tsx`'s `friendlyError()`
(in `frontend/src/api.ts`) currently pattern-matches the error *text* against its own small
keyword list and replaces anything that doesn't match with a fully generic line — which means it
would silently undo Phases 3/4's fix for the exact UI surface (Matter Search chat) that motivated
this whole document, re-genericizing a message the backend just worked to make specific. Verified
this already happens today: a disabled-Bedrock-model error reaches the frontend as
`"Bedrock Converse failed (...)"`, which doesn't match `friendlyError()`'s keyword list and gets
replaced with "The AI service had a problem generating this summary. Please try again" — wrong
advice for a permanent config problem.

**Resolution:** once a failure is classified by the Phase 1 catalog, the job/response error the
frontend receives should already be final, user-ready text — not something the frontend
re-judges. Add a boolean alongside the existing `error` field (job status response and any
synchronous error response that carries a catalog-classified message) — e.g. `error_is_friendly`
— set `true` whenever `handlers.py` sets the error from the catalog (Phase 3 item 3, Phase 4 item
1), `false` for the Phase 3 item 1 catch-all's deliberately generic 500 and any other raw,
unclassified exception. The frontend shows `error` as-is when `error_is_friendly` is `true`, and
falls back to `friendlyError()`'s existing keyword-matching only when it's `false` or absent —
so `friendlyError()` keeps doing its original job (a safety net for the genuinely unclassified
residual case) instead of fighting the backend's new specificity. This is additive to the
existing response/job shape, consistent with the Anti-goals ("extending these is fine;
renaming or restructuring them is not"). Phase 6's generalization of "`friendlyError()`-style
treatment" to other panels means: read `.error` and `error_is_friendly` the same way everywhere,
not literally call the same keyword-matching function from every panel.

**The health-probe pre-loop-gap question.** Phase 3 item 1 originally scoped the new API
catch-all narrowly around the synchronous route-dispatch path, describing `_run_health_probe` as
already safe by analogy — but `_run_health_probe`'s *own* body (`_get_deps()`, constructing
`SecretsClient`, calling `run_checks(...)`) has no try/except around it; only the per-connector
probes *inside* `run_checks()` are individually caught. This is the same "pre-loop gap" shape
Phase 5 fixes for Quarterly Reports/AML Export/Sanctions Refresh, and the health probe is itself
an unattended, EventBridge-scheduled invocation. **Resolution:** Phase 3 item 1's catch-all wraps
`_run_health_probe`'s own body too, not just the synchronous route-dispatch path — cheap, and
consistent with this document's own stated goal that scheduled/unattended jobs never fail
silently.

## The six phases (decided scope, order, and grouping)

Originally proposed as 8 sub-projects in the issue; grouped here into 6 right-sized phases
(grouping is this document's call, to avoid PRs too small to be worth their own review pass).
Order follows real dependency, not just the issue's listing order.

### Phase 1 — Shared error-classification taxonomy + message catalog (backend)

Foundational. Generalizes the existing pattern already proven in `suggest_fix()`
(`backend/api/integrations.py`): a deterministic, pattern-matched mapping from a raw error
detail to {classification, plain-language message, suggested action}. Classifications: transient
/ retryable, permanent / misconfiguration, needs-reauth, unknown. Lands in `services/` so it's
importable by every backend Lambda and the API handlers — but explicitly NOT by the installer
(see Anti-goals). `suggest_fix()` itself should come to use this catalog internally rather than
duplicating its own pattern list, once the catalog exists.

**What's left to the builder:** exact module location and function signatures within `services/`;
whether `suggest_fix()` is refactored to call the new catalog or left as-is with the catalog
added alongside it (prefer refactoring it to call the catalog, to avoid two parallel pattern
lists drifting apart — but confirm this is a small, safe change before doing it).

### Phase 2 — Deploy-time (`terraform apply`) failure classification

The original ask. The plumbing already exists: `installer/function/handler.py`'s
`_failure_message()` already prefers a specific message written to an SSM parameter
(`LAST_ERROR_PARAM`) by the deploy script over the generic `BUILD_FAILED_MSG = "Deployment
failed"` fallback. The actual gap is that whatever runs `terraform apply` (the deploy script
invoked by CodeBuild — confirm the exact file, likely `installer/deploy.sh` or logic it calls)
never writes anything specific there for a failed `apply`, only (apparently) for other failure
modes already covered. This phase adds pattern-matching over `terraform apply`'s own
stderr/output for common AWS failure signatures (quota exceeded, service limit, insufficient
permissions, billing/payment issue) and writes a specific message + customer action to
`LAST_ERROR_PARAM` before the script exits non-zero, mirroring the Bedrock pre-flight check's
message quality. This is a small, standalone implementation (stdlib-only, per Anti-goals) that
follows Phase 1's taxonomy conceptually without importing its code.

Confirmed during builder-check: `installer/deploy.sh`'s `on_error` trap already writes a generic
`"Deployment failed at step $STEP"` to `LAST_ERROR_PARAM` for *any* failed step (including
`terraform apply`) unless that step sets `ERR_MSG`/calls `fail()` first — exactly what the
Bedrock pre-flight step already does (lines ~241-257). `do_apply()` (lines ~276-281) is where this
phase's new classification logic plugs in, following that exact existing pattern.

**What's left to the builder:** which AWS error signatures are worth pattern-matching for v1
(start with the ones recon identified — quota/limit/credit/billing — and confirm via AWS
documentation or past deploy failures, don't invent an exhaustive list).

### Phase 3 — Backend request-handling robustness + connector consistency

Three related, backend-only fixes, grouped because each is individually small:

1. **API catch-all.** `backend/api/handlers.py`'s synchronous route-dispatch path inside
   `handler()` (the branch reached when the event is neither `worker` nor `health_probe`) has no
   top-level `try/except`, unlike `_run_worker`, which already catches everything. One unhandled
   exception in any route currently produces an undocumented raw Lambda crash instead of a JSON
   error response. Add the same discipline: wrap the dispatch, return a generic 500 JSON error
   body in the existing `{"error": "..."}` shape `_response()`'s other callers already use
   (confirmed during builder-check as the pervasive convention) — do not leak internals — log the
   real exception server-side, and set `error_is_friendly: false` (see "Coordination across
   phases" above). **Also wrap `_run_health_probe`'s own body** (not just its per-connector probes,
   which `run_checks()` already isolates) the same way, per the cross-phase resolution above.
2. **Graph connector 401 bug.** Replace the four `"401" in str(err)` substring checks in
   `backend/connectors/graph/connector.py` (lines ~121, 140, 334, 374) with a correct check
   against the actual HTTP status code / exception type the Graph SDK exposes, not a string
   search that can false-positive on an unrelated error containing the digits "401".
3. **Route live Matter Search errors through the catalog.** Today, `suggest_fix()` is only
   reachable from the scheduled health-probe path; a live Matter Search failure shows the raw
   `HttpError` string instead of the same plain-language translation. Thread the Phase 1 catalog
   (or `suggest_fix()`, once it calls the catalog) into the live search failure path so the same
   translation staff already see on the Integrations tab also appears when a search fails live.

**What's left to the builder:** confirm what the Graph SDK actually exposes for a 401 (a status
code attribute, an exception subtype) at each of the four call sites before replacing the
substring check — the connector's own line ~416 already does this correctly via
`response.status_code == 401` and is a confirmed-working model to follow.

### Phase 4 — Bedrock/LLM runtime parity + surfacing partial-answer degradation

Two related gaps in the synthesis path:

1. **Runtime model-access parity.** The installer's Bedrock pre-flight check already gives a
   good message ("model X isn't accessible in region Y — enable it in the Bedrock console, then
   retry"). A runtime Bedrock failure during a live Matter Search for the same underlying cause
   (model not enabled) should give an equally specific message, not a generic "please try again"
   — which is actively wrong advice for a permanent configuration problem that retrying cannot
   fix.
2. **Surface partial-answer degradation.** This is narrower than originally described in recon:
   the data already exists end-to-end. `SynthesisResult.parse_ok` and `missing_sections`
   (`backend/search/synthesize.py`, threaded through `backend/api/pipeline.py`) are already
   computed and already typed on the frontend (`frontend/src/types.ts`), but no frontend
   component ever reads `missing_sections` — `StatusReport.tsx`'s `generationFailed` check only
   catches a *total* parse failure (`parse_ok === false` or zero sections), not a *partial* one
   (valid JSON, parsed fine, but some expected sections are absent). A partial answer currently
   renders as if it were complete. Add a banner (follow the existing `banner-warn` pattern
   already used for `insufficient_info`) when `missing_sections` is non-empty, naming which
   sections are missing.

**What's left to the builder:** where exactly in the live-search call path a Bedrock
model-access error needs to be caught and classified (likely in `backend/search/orchestrator.py`
or wherever the Bedrock client call itself is invoked — confirm); exact wording for the new
missing-sections banner, following this codebase's existing banner-copy voice.

### Phase 5 — Unattended-job failure visibility

Three jobs, one theme — a scheduled job must never fail with zero trace in the portal:

1. **Quarterly Reports + AML Export: close the pre-loop gap.** Both jobs already isolate and
   report per-item failures correctly inside their main loop, and both already call a `notify()`
   that publishes to SNS (`backend/lambdas/quarterly_report/handler.py`,
   `backend/lambdas/aml_export/handler.py`). The gap is a failure *before* that loop starts (a
   connector fully down during discovery, a config-load error) — today that never reaches
   `notify()` at all. Wrap each job's top-level body so any failure, wherever it occurs, still
   calls `notify()` (or an equivalent error-path variant of it) with a clear summary of what
   failed and why, instead of leaving the run silent.
2. **Sanctions Refresh: add an alerting path.** `backend/lambdas/sanctions_refresh/handler.py`
   has no SNS/notification mechanism at all today, even in principle — the three source fetches
   (UN Consolidated, Canada SEMA, Canada Public Safety) each already catch and log their own
   failures independently (`backend/screener/fetcher.py`), but nothing downstream of that log
   line ever reaches a human. Add the same `notify()`-style SNS publish pattern the other two
   scheduled jobs use, for both a total-failure case and a partial case (e.g. 2 of 3 sources
   refreshed, one didn't).
3. **Sanction Screener: surface bundle staleness (warn, never block — see Constraints).**
   `backend/screener/fetcher.py`'s `load_from_bundle()` treats the packaged bundle as the primary
   loader with no freshness signal exposed anywhere; confirm exactly how/whether a freshly
   S3-refreshed bundle is distinguished from the packaged fallback in the actual screening path
   (`backend/screener/screening.py` or wherever screening actually runs), then surface the
   bundle's `generated_at` date (already present in the bundle's own JSON, per `fetcher.py`'s
   logging) to staff as a banner whenever screening ran against a bundle past some staleness
   threshold — pick a reasonable default (e.g. older than the monthly refresh cadence plus a
   grace window) and state it as this phase's own call.

Confirmed during builder-check: `terraform/modules/sanctions_refresh` has no SNS resource at all
today — this phase adds one from scratch, following the same pattern as the Quarterly
Report/AML Export modules' existing SNS topics. The packaged-vs-S3-refreshed bundle decision
lives in `backend/api/screener.py`'s `ScreenerService._load_bundle()` (not `fetcher.py`, which is
only used by the refresh Lambda itself) — it tries S3 first when `sanctions_bucket` is
configured, falls back to the packaged bundle on any exception, and already logs `generated_at`
either way, which the new staleness banner can read directly.

**What's left to the builder:** the exact staleness threshold for the screener banner (pick a
reasonable default — e.g. older than the monthly refresh cadence plus a grace window — and state
it as this phase's own call).

### Phase 6 — Frontend error-handling consistency

Every panel already uses the same `catch → setError(e.message)` idiom, and `banner-error` already
exists as a shared visual pattern — but `frontend/src/api.ts`'s read/list calls never read the
backend's `.error` response body (only the mutating calls currently do), so the same red banner
sometimes shows a specific backend-provided reason and sometimes a bare `"Failed to load (503)"`,
with no way for staff to tell which case they're in. Matter Search's `friendlyError()` + retry
button (`frontend/src/components/MatterSearchChat.tsx`) is the one place this is already done
well — generalize that pattern (read `.error` from every call, not just mutations) across
`api.ts`, so every panel's error banner is as specific as the backend already made it.

Confirmed during builder-check: the read/list vs. mutating split matches this description exactly
— e.g. `listReports`, `listActivity`, `listAmlExports`, `listMatters`, `listCertificates` skip
`.error` today; `updateConfig`, `connectEstateably`, `clioExchange`, `generateCertificate`, etc.
already read it. Note: `fetchConfig` is a GET that already reads `.error` by deliberate existing
design (it has its own explaining comment) — not a bug, just worth the builder noticing when
enumerating the actual list so it isn't "fixed" twice.

**What's left to the builder:** enumerate the complete list of `api.ts` functions still skipping
`.error`; whether `friendlyError()` itself should become a shared utility all panels call, or
whether each panel keeps its own `setError(e.message)` now that `e.message` itself will reliably
carry the backend's specific reason (prefer the latter — smaller diff, consistent with the
Non-goals' "don't redesign the visual language" — and consistent with the "Coordination across
phases" resolution: every panel should read `error`/`error_is_friendly` the same way, not
necessarily call one shared function).

## What's left to the builder (plan-level, across all phases)

- Per-phase task breakdown and file-level task boundaries — this document sizes and sequences
  the work; `superpowers:writing-plans` produces the actual task list per phase, same as the
  deployment-config and first-run-setup-wizard plans.
- The specific AWS error signatures for Phase 2, the staleness threshold for Phase 5, and the
  complete `api.ts` function list for Phase 6 — each noted in its own phase section above.

(A builder-check pass on this document, done before handoff, resolved every other open question
it raised — either by reading the code directly, or via the "Coordination across phases" section
above. See that section for the two decisions it required: the `error_is_friendly` field, and
extending Phase 3's catch-all to `_run_health_probe`.)
