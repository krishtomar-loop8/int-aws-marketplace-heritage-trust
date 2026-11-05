# Aphrodite — AWS Marketplace Readiness Plan

**Duration:** 20 working days · **Team:** 3–4 engineers working in parallel tracks (see
Section 2) · **Goal:** take Aphrodite from "cleaned-up single-customer codebase" (current
state, post-rebrand) to a **submittable AWS Marketplace listing** — a product a new buyer
can subscribe to, deploy into their own AWS account, configure, and start using, with
Loop8 able to support and bill them without bespoke engineering per customer.

> **This is a team plan, not a solo plan.** At 1 engineer, this scope is realistically
> 2–4 person-months, not 20 days. Every day range below assumes 3–4 engineers working
> largely independent tracks, with explicit integration checkpoints and buffer days
> rather than one person moving through phases sequentially.

> **Scope honesty check:** 20 days is enough to get the *product* ready to submit. AWS's
> own Marketplace technical + business review (after submission) is outside our control
> and historically takes **1–6 additional weeks**. This plan also assumes a **private-offer
> / assisted-onboarding v1** (Loop8 still helps a new customer through Clio/QuickBooks/
> Entra app registration) rather than a fully zero-touch self-service signup — full
> self-service is called out as a fast-follow in the roadmap, not squeezed into 20 days.

---

## 0. Where we start from

Already done (prior session): renamed all Heritage-Trust-specific naming to `aphrodite`,
removed confidential engagement metadata (AWS account ID, partner funding tag, client
contact names), emptied client-specific operational defaults, swapped branding to a
generic text wordmark, rewrote docs, migrated to a fresh repo.

**Not yet done** (this is what the 20 days covers): the deep business logic — system
prompt, AML taxonomy, letterhead — is still hardcoded "Aphrodite-as-the-customer"
rather than configurable per deployment; there is no self-service onboarding; there is
no AWS Marketplace delivery artifact (CloudFormation entry point); there is no billing/
metering integration; there has been no fresh-account end-to-end validation; there is no
security/compliance documentation package.

---

## 1. Single-tenancy & deployment model (read this first — it shapes every phase)

**Decision carried forward from the architecture:** Aphrodite is **bring-your-own-account
(BYOA) single-tenant** — each customer gets a dedicated, fully isolated stack deployed
into *their own* AWS account (not a shared multi-tenant SaaS backend Loop8 hosts). This
is deliberate and should stay:

| | Shared multi-tenant SaaS | **BYOA single-tenant (chosen)** |
|---|---|---|
| Data isolation | Logical (row-level), harder to prove to auditors | Physical — a different AWS account per customer |
| Fits regulated buyers (trust companies, BCFSA/PIPEDA-style) | Needs heavy compliance work | Natural fit — customer's own data residency/KMS keys |
| Engineering cost | One deploy to maintain | **N deployments to version, patch, and support** |
| Billing | Centralized metering is trivial | Needs AWS Marketplace Metering/Entitlement Service per account |
| Onboarding friction | Low (shared signup) | Higher — each buyer needs their own Clio/QuickBooks/Entra app registrations |

**What this means operationally, and what the plan must cover:**
- A **fleet management problem**: N customer accounts, each on its own Terraform state
  and (eventually) its own template version. Need a versioning + update strategy (Phase 4).
- A **support access problem**: Loop8 needs a safe way to diagnose a customer's deployment
  without full account access. Needs a scoped, auditable cross-account support role
  (Phase 5).
- A **cost-per-customer problem**: each customer pays their own AWS bill (Lambda, Bedrock
  tokens, S3) *plus* the Aphrodite license fee to Loop8. The Marketplace listing must be
  explicit about this split so buyers aren't surprised (Phase 6/8).
- **Not** a problem this plan solves: horizontal scaling of one shared backend — there
  isn't one. Each stack only has to scale to one customer's usage.

---

## 2. Execution model: resourcing, parallel tracks & buffer

**Resourcing assumption: 3–4 engineers**, not one. At 1 engineer, this scope (config
refactor + onboarding UI + CFN automation + billing + security hardening + QA + listing
prep) is genuinely 2–4 person-months of work and 20 calendar days is not realistic. At
3–4 engineers working in parallel tracks, it's tight but achievable — which is the
assumption the rest of this plan now makes explicit.

**Day 1, before anything else: de-risk the one unproven assumption.** Phase 4
(CloudFormation-triggers-Terraform via an async custom resource) is the pattern the
whole packaging phase depends on and the one piece of this plan nobody here has built
before. Spend part of Day 1 prototyping just that mechanism in isolation — a minimal
custom resource that kicks off a CodeBuild job and signals success/failure back to
CloudFormation — **before** committing the Day 10 integration checkpoint date to it. If
it turns out messier than expected, that needs to be known on Day 1, not discovered on
Day 13.

### Tracks

| Track | Owner | Covers | Mostly independent of |
|---|---|---|---|
| **Track 1 — App/Config** | Eng-1 | Phase 1 (genericization), then Phase 2 (modular toggles) | Tracks 2 & 3 (different files/layers) |
| **Track 2 — Onboarding** | Eng-2 | Phase 3 (self-service onboarding UI + OAuth callbacks) | Tracks 1 & 3 until the Day 7 integration point |
| **Track 3 — Packaging & billing** | Eng-3 (+ Eng-4 if 4th engineer) | Phase 4 (CFN template, CodeBuild, versioning, metering/entitlement) | Tracks 1 & 2 — only needs *a* tagged Terraform ref, not the final one, until integration |
| **Track 4 — Platform/Security** (folds into Track 3 if only 3 engineers) | Eng-4 | Billing/metering half of Phase 4, then leads Phase 5 security consolidation | Can start CI security scanning (pip-audit/npm audit/gitleaks) Day 1, independent of everything |

**Why this split works:** Phases 1–3 touch almost entirely different parts of the
codebase (Python business logic / connector OAuth+React setup UI / AWS packaging
plumbing), so three engineers can work without constantly colliding on the same files.
The one real shared surface is `terraform/variables.tf` — Track 1 and Track 3 both touch
it, so agree on the variable names/shape by end of Day 1 and then work independently.

**If only 3 engineers are available:** merge Track 4 into Track 3 (Eng-3 owns both CFN
packaging and billing/metering sequentially instead of in parallel) and push the Phase 5
security work entirely into the Days 11–15 all-hands window below, rather than having a
dedicated engineer start it early.

### Buffer

Five days of this plan (25%) are explicit buffer/integration checkpoints, not additional
scope:

- **Day 7** — first integration checkpoint: Tracks 1 & 2 merge into main; Track 3
  re-points its CFN template at the now-current Terraform.
- **Day 10** — second integration checkpoint: full-team dry run of a real CFN deploy +
  onboarding flow together. This is the day that absorbs any Track 3 slippage from the
  Day 1 spike being harder than expected.
- **Days 14–15** — buffer after the Phase 5 security push, for fixing whatever the audit
  finds.
- **Days 18–19** — buffer after Phase 6 QA, for fixing whatever fresh-account testing
  finds (this is traditionally where the ugliest surprises land, so it has the most
  slack).

If a track finishes early, that engineer doesn't sit idle — Days 10, 14–15, and 18–19 are
exactly where early-finishers should start pulling forward Phase 5 audit work or Phase 7
listing-copy drafting, both of which don't require the full stack to be final.

---

## 3. Phase plan (task detail per phase)

### Phase 1 — Configuration & business-logic genericization (Track 1, Days 1–3)

**Agenda:** Turn the remaining hardcoded "Aphrodite is the customer" business logic into
per-deployment configuration: the search agent's system prompt (`backend/search/
framework.py`), the AML own-company-exclusion name and role taxonomy (`quality.py`,
`export.py`, `designations.py`), the quarterly-report letterhead/signoff (`render_docx.py`),
and the SharePoint HR/Finance scoping rules (`scoping.py`). Introduce one deployment
config surface (Terraform variables → Lambda env vars, with a documented schema) that
captures everything that varies per customer: org display name, letterhead address,
own-company name for AML exclusion, qualifying Clio practice areas, SharePoint sites.

**Problem it solves:** Today the code only works correctly for one specific customer's
business rules. You cannot onboard a second buyer without an engineer editing Python.
This is the single biggest blocker to calling this a "product."

**API/integration changes:** None to the external APIs (Clio/Estateably/QuickBooks/Graph)
themselves — this is purely about pulling constants out of code and into config.

**Difficult parts:**
- The AML taxonomy and letterhead logic encode real compliance judgment calls (which
  roles get screened, what counts as a "trust matter"). Making these configurable without
  making them *silently wrong* for a new customer who doesn't fill in every field is the
  hard part — needs safe defaults + explicit "unconfigured" flags surfaced in the UI
  rather than silent fallback to Aphrodite's own defaults.
- The system prompt (`framework.py`) is a large, carefully-tuned block of prompt
  engineering. Parameterizing the org name/address inside it without breaking the tuning
  requires careful testing of real queries before/after.

**Tasks:**
1. Inventory every hardcoded business rule/constant across `backend/search/framework.py`,
   `backend/search/scoping.py`, `backend/lambdas/aml_export/quality.py`, `export.py`,
   `designations.py`, and `backend/lambdas/quarterly_report/render_docx.py`.
2. Define the deployment config schema — one documented list of every value that varies
   per customer: org display name/legal name, letterhead address/phone, privacy-policy
   URL, own-company exclusion name(s), Clio qualifying practice areas, Clio exclusion
   field name, SharePoint site URLs, HR/Finance folder-scoping patterns, and the
   system-prompt's org facts (office address, assistant name).
3. Refactor `render_docx.py`'s `_LETTERHEAD_NAME`/`_LETTERHEAD_ADDRESS`/
   `_SIGNOFF_COMPANY`/`_PRIVACY_NOTICE` into env vars with a visibly-flagged "not
   configured" fallback in the generated draft (never a silent wrong value).
4. Refactor `quality.py::is_excluded_party_name` to read the own-company name(s) from
   config instead of a hardcoded string match.
5. Refactor `scoping.py`'s HR/Finance exclusion rules into configurable folder-name
   patterns.
6. Refactor `framework.py`'s system prompt to template in org name/address/assistant
   naming from config; re-run a representative query set before/after to confirm prompt
   quality holds.
7. Add the corresponding Terraform variables and wire them through to each Lambda's
   environment block.
8. Add/extend unit tests covering the "unconfigured" default path (e.g., empty
   own-company name ⇒ nothing excluded, not a crash).
9. Update each module's README with a table of the new config knobs (env var ↔
   Terraform var ↔ what it controls).
10. Audit the entire codebase (Lambda code + Terraform defaults) for any remaining
    pointer back to Loop8's own AWS account or shared resources — a shared Bedrock
    Knowledge Base ID, a shared KMS key ARN, any hardcoded ARN left over from
    development — and confirm every resource the app touches is provisioned fresh by
    the customer's own Terraform apply. (See Section 6, point 2.)

---

### Phase 2 — Modular, toggleable architecture (Track 1, Days 4–7)

**Agenda:** Make every connector (Clio, Estateably, QuickBooks, SharePoint/Graph) and
every module (Matter Search, Quarterly Reports, AML Export, Sanction Screener)
independently enable/disable via Terraform flags, with conditional resource creation
(`count = var.x_enabled ? 1 : 0`) and the frontend nav hiding disabled modules. Verify the
search orchestrator degrades gracefully (not a crash) when a connector is absent.

**Problem it solves:** Not every buyer uses Estateably, and not every law firm wants the
AML/Sanction Screener modules. Marketplace buyers expect to configure (and ideally price)
only what they use; today the Terraform root always wires up all four connectors.

**API/integration changes:** Connector construction (`backend/api/handlers.py::
_build_deps`) needs to tolerate missing secrets/config instead of assuming all four exist.
Each connector needs a lightweight "is this configured?" check surfaced to both the
backend fan-out logic and the Integrations health tab.

**Difficult parts:** The matter-search orchestrator currently fans out to all sources in
parallel and synthesizes one answer — disabling a source changes what "complete" means
for a result, and the LLM synthesis step needs to know which sources were intentionally
skipped vs. which failed, so it doesn't report a false gap.

**Tasks:**
1. Audit `terraform/main.tf` for every module instantiation and map which Terraform
   module needs which connector secret(s).
2. Add `*_enabled` Terraform variables for each connector (Clio, Estateably, QuickBooks,
   Graph/SharePoint) and confirm each module's own enable flag (quarterly/AML/screener
   already partially exist; extend to a full toggle including resource creation, not just
   schedules).
3. Wrap each connector's Secrets Manager entry + IAM policy statements in
   `count`/`for_each` conditionals keyed off the new flags.
4. Update `backend/api/handlers.py::_build_deps` to construct only enabled connectors and
   expose an "enabled sources" set to callers instead of assuming all four exist.
5. Update `backend/search/orchestrator.py` to skip disabled sources in the fan-out and
   distinguish "intentionally skipped" from "errored" in the result set.
6. Update `backend/search/synthesize.py` so the LLM synthesis step states which sources
   were not queried by design vs. which failed, instead of reporting a false gap.
7. Update `frontend/src/nav.tsx` / `Sidebar.tsx` / `config.ts` to hide disabled modules
   based on a capability flag returned by the API.
8. Update the Integrations health panel to only list enabled connectors.
9. Add an integration test that disables each connector one at a time and asserts correct
   degraded behavior (no crash, accurate "not queried" messaging).
10. Document the module/connector toggle matrix in `terraform/README.md`.

---

### Phase 3 — Self-service onboarding experience (Track 2, Days 1–9)

**Agenda:** Replace the engineer-run `scripts/clio_oauth_bootstrap.py` /
`quickbooks_oauth_bootstrap.py` CLI flow with a guided setup experience: either a
lightweight setup web page (an admin-only route in the existing React app) or a polished
CLI wizard, that walks a new customer's admin through connecting each enabled source,
validates each connection live (reusing the existing Integrations health-check logic),
and writes the resulting secrets to Secrets Manager. Pair this with step-by-step written
guides for the parts that can't be automated (creating a Clio Private App, QuickBooks
app, Entra enterprise app + admin consent, SharePoint `Sites.Selected` grants).

**Problem it solves:** Today, bringing on a new customer requires a Loop8 engineer to run
Python scripts by hand with the customer's credentials in front of them. That doesn't
scale past a handful of customers and isn't something a Marketplace buyer expects.

**API/integration changes:** OAuth redirect handling needs a hosted callback endpoint
(API Gateway route) instead of the current out-of-band manual-copy-paste flow for Clio;
QuickBooks' real redirect URI needs to point at this new endpoint instead of a local
script.

**Difficult parts:** Clio and QuickBooks both require the *customer* to be the one who
creates and owns the OAuth app in their own developer account (so app ownership stays
with them, not Loop8) — this is inherently a human, multi-step, cross-vendor process we
don't fully control, and realistically still needs a human (customer admin or Loop8
support) in the loop, not a fully automated signup. Treat v1 as "assisted self-service,"
not zero-touch.

**Tasks:**
1. Design the end-to-end onboarding sequence: admin login → per-connector setup screen →
   OAuth redirect → callback → validate → save to Secrets Manager → health check green.
2. Build an API Gateway + Lambda OAuth callback endpoint for Clio, replacing the manual
   out-of-band authorization-code copy/paste in `scripts/clio_oauth_bootstrap.py`.
3. Refactor the Clio bootstrap logic out of the CLI script into a reusable function the
   new callback Lambda calls.
4. Do the same for QuickBooks: build a callback endpoint, point the real OAuth redirect
   URI at it, refactor `quickbooks_oauth_bootstrap.py`'s logic into a reusable function.
5. Build an admin-only "Setup" screen in the React app: a card per connector (Not
   connected / Connected / Error) with a "Connect" button that starts each OAuth flow.
6. Add a SharePoint/Graph card with step-by-step admin-consent instructions (this leg
   can't be fully automated — document the deep link + required consent screen).
7. Wire each "Connect" card to the existing Integrations health-check logic for live
   validation feedback after connecting.
8. Add a config step (UI form or a clearly-commented `deployment.tfvars.example`) for the
   Phase 1 business-config fields so an admin doesn't have to hand-edit Terraform.
9. Write the customer-facing guides: "Create a Clio Private App," "Create a QuickBooks
   app," "Register an Entra enterprise app + grant admin consent," "Grant Sites.Selected
   to the SharePoint app."
10. Dry-run the full onboarding flow against sandbox Clio/QuickBooks accounts, capture
    friction points, and fold fixes back into the UI copy/guides.

---

### Phase 4 — AWS Marketplace packaging & deployment mechanism (Track 3/4, Days 1–10)

**Agenda:** Build the actual Marketplace delivery artifact. AWS Marketplace's native
delivery methods are AMI, container, SaaS (seller-hosted), or **CloudFormation
template** — it has no first-class concept of "a multi-module Terraform repo." Build a
thin CloudFormation template that is the thing AWS Marketplace deploys when a buyer
subscribes: it provisions a CodeBuild project + pipeline that pulls the versioned
Terraform from this repo and runs `terraform init/apply` into the buyer's account,
surfacing outputs (SPA URL, Cognito domain) back through CloudFormation outputs. Define
a semantic versioning + changelog strategy, and a safe re-apply path for delivering
updates to already-deployed customers.

**Problem it solves:** Without this, there is no way to actually list and deploy the
product through AWS Marketplace — this phase *is* "becoming an AWS Marketplace product"
mechanically.

**API/integration changes:** None to Clio/Estateably/etc. — this is AWS-internal
(CloudFormation ⇄ CodeBuild ⇄ Terraform ⇄ target account).

**Difficult parts — this is the hardest phase in the plan:**
- CloudFormation-triggers-Terraform is a well-worn pattern among ISVs but has real sharp
  edges: CloudFormation custom resources have a 1-hour execution limit and poor native
  retry/rollback semantics for a long-running `terraform apply`; failures mid-apply can
  leave the buyer's stack in a half-provisioned state that's confusing to roll back from
  a CloudFormation delete.
- The one-time bootstrap (`terraform/bootstrap/`) currently requires the customer to run
  an `aws cloudformation deploy` themselves with admin credentials — this has to be
  reconciled with (or absorbed into) the Marketplace-triggered flow so the buyer doesn't
  do two separate manual AWS CLI steps.
- Versioning N independently-deployed Terraform states safely (upgrading customer A to
  v1.2 shouldn't be able to touch customer B, and a bad version needs a rollback story)
  is genuinely hard and under-specified in most "Terraform as a Marketplace product"
  write-ups — expect to spend real design time here, not just implementation time.

**Tasks — building the CloudFormation template:**
1. Define the template's `Parameters` block: everything a buyer fills in at
   subscribe/launch time (deployment region, environment name, notification email,
   Terraform version/ref to deploy, and the Phase 1 business-config basics if not
   deferred to Phase 3's setup UI).
2. Design the resource graph: an S3 bucket for Terraform artifacts/state bootstrap, an
   IAM role for CodeBuild scoped to exactly what Terraform needs (reuse/extend the
   existing `terraform/bootstrap/bootstrap.yaml` policy as the baseline), and a
   CodeBuild project that checks out the pinned Terraform ref and runs
   `terraform init && terraform apply` with the CFN parameters passed through as
   `TF_VAR_*` environment variables.
3. Fold the existing one-time bootstrap (`terraform/bootstrap/bootstrap.yaml` — state
   bucket, OIDC provider, deploy role) into this template so the buyer runs **one**
   CloudFormation deploy, not a separate manual `aws cloudformation deploy` step first.
4. Build the custom-resource Lambda that triggers the CodeBuild run on `Create`/`Update`/
   `Delete` CloudFormation events.
5. Because a `terraform apply` can run well past Lambda/custom-resource time limits,
   make the trigger **asynchronous**: the custom resource starts the CodeBuild build and
   returns immediately; a separate poller (EventBridge rule on CodeBuild state change →
   Lambda) sends the final `SUCCESS`/`FAILED` signal back to CloudFormation's pre-signed
   response URL.
6. Implement the `Delete` path: trigger `terraform destroy` via the same CodeBuild
   mechanism; decide and document what happens to data-bearing resources with deletion
   protection / Object Lock (likely: require manual confirmation, don't auto-delete).
7. Surface the Terraform outputs that matter to the buyer (SPA URL, Cognito domain) back
   through CloudFormation `Outputs` — have the CodeBuild step write them to SSM Parameter
   Store, and have the custom resource read them back for the `Outputs` block.
8. Define the versioning scheme: tag the Terraform repo per release, make the CFN
   template's Terraform-ref parameter point at a tag, and start a `CHANGELOG.md`.
9. Test the `Update` path specifically: re-running the stack with a newer version tag
   should re-trigger `terraform apply` against the *same* state without destroying
   customer data.
10. Test `Create`, `Update`, and `Delete` end-to-end in a clean sandbox AWS account before
    moving to Phase 6's full fresh-account QA.
11. Decide explicitly whether v1 ships automated custom-domain support or not: the
    current custom-domain flow requires a human DNS step between two Terraform applies
    (create the ACM cert, wait for a manually-added validation CNAME, then attach the
    domain to CloudFront), which doesn't fit a one-shot CloudFormation `Create`. Either
    design the custom resource to pause/resume across that step, or ship v1 with the
    CloudFront default domain only and a documented manual follow-up for a custom
    domain. (See Section 6, point 3.)
12. Size the custom-resource polling/timeout logic (task 5) for **CloudFront
    distribution propagation specifically** (15–45 minutes), not just for `terraform
    apply`'s own runtime.
13. Audit every stateful resource (DynamoDB tables: jobs, activity log, integration
    health; S3 buckets: outputs, reference) for retention behavior on stack `Delete` or a
    failed `Create` rollback. Enable DynamoDB point-in-time recovery on all tables, and
    confirm the outputs bucket's existing versioning/Object Lock actually prevents data
    loss in both failure paths, not just on a clean teardown. (See Section 6, point 6.)
14. Decide and document what survives a stack `Delete`: the likely answer is
    compute/API resources are destroyed but the outputs bucket and DynamoDB tables are
    retained (orphaned, still billed to the customer) unless they explicitly opt into
    full teardown — make this an explicit, customer-facing decision, not an accident of
    whatever Terraform's default behavior happens to be.
15. Add a config-schema compatibility check to the version-update path (task 9): a
    version update must not silently misread an older customer-edited config artifact
    (the AML designation list, the account-classification workbook) in S3.

**Tasks — billing & entitlement:**
16. Decide the pricing model (flat subscription vs. usage-based dimensions) with
    whoever owns commercial/pricing decisions.
17. If usage-based: build a scheduled Lambda that aggregates usage from the existing
    activity/audit tables and calls `aws-marketplace:MeterUsage` on a fixed cadence.
18. If flat: implement an `aws-marketplace:ResolveCustomer` + `GetEntitlements` check
    (at first setup or login) to confirm the buyer is actually entitled before the app
    becomes usable.
19. Test the chosen integration against the AWS Marketplace metering/entitlement
    sandbox before submission.

---

### Phase 5 — Security hardening & compliance readiness (All tracks, Days 11–15)

**Agenda:**
- **IAM least-privilege audit** of every role the Terraform creates (the bootstrap CFN's
  curated policy is a good baseline — audit the per-module IAM too).
- **Dependency/vulnerability scanning** — `pip-audit` on the Python deps, `npm audit` on
  the frontend, wired into CI so it gates merges, not just a one-time check.
- **Secrets hygiene** — confirm nothing is ever logged (the AML export's "no PII to logs"
  discipline already exists; extend that review to every Lambda).
- **Cross-account support role** — a narrowly-scoped, read-only, time-boxed IAM role a
  customer can optionally grant Loop8 support for diagnostics, fully logged via CloudTrail
  on the customer side.
- **Data-flow documentation** — a diagram + written data-flow doc covering exactly what
  leaves the deployment region and when (this already exists informally for the Bedrock
  cross-region decision; formalize it for the Marketplace security questionnaire / CAIQ).
- **Encryption review** — confirm KMS CMK usage and TLS enforcement are consistent across
  every new flag/toggle added in Phases 1–2 (easy to accidentally skip on a new
  conditionally-created resource).

**Problem it solves:** AWS Marketplace's technical review will reject a listing with
IAM/security gaps, and the target buyers (regulated trust/legal firms) will very likely
run their own vendor security review independently — this phase has to satisfy both.

**Difficult parts:** Regulated buyers often want a SOC 2 report or formal pen-test
results, neither of which can be produced in 20 days — the realistic Day-20 deliverable
is a strong self-assessment (CAIQ-style questionnaire + architecture/data-flow docs), with
SOC 2 / third-party pen-test flagged honestly as a post-launch roadmap item, not promised
at submission.

**Tasks:**
1. Enumerate every IAM role/policy Terraform creates (including anything new from
   Phases 2 and 4); diff actual permissions used against permissions granted and tighten
   any wildcard actions.
2. Add `pip-audit` and `npm audit --audit-level=high` to `.github/workflows/ci.yml` as a
   required, merge-blocking check (not a one-off manual run).
3. Add a secrets-scanning check (e.g., gitleaks) to CI to catch credential leaks going
   forward.
4. Review every Lambda's log statements for inadvertent PII/secret logging, extending the
   AML export's existing "no PII to logs" discipline repo-wide.
5. Design and implement the scoped cross-account support IAM role: read-only, time-boxed,
   optional for the customer to attach, fully auditable via their own CloudTrail.
6. Write the data-flow document/diagram: every point data crosses a region or account
   boundary (including the Bedrock cross-region inference option) and why.
7. Fill out a CAIQ-style (or AWS's own Marketplace security questionnaire) self-assessment
   from the artifacts above.
8. Audit every conditionally-created resource from Phase 2 for consistent KMS CMK +
   TLS-enforcement coverage (easy to miss on a newly-toggleable resource).
9. Run `checkov`/`tfsec` (or AWS Config conformance packs) against the Terraform and
   remediate findings.
10. Document the SOC 2 / pen-test gap explicitly, with a stated remediation timeline, for
    buyer-facing transparency rather than silence.
11. Document explicitly (for the team, and eventually the buyer-facing security
    questionnaire) why connector OAuth secrets cannot be collected as CloudFormation
    parameters: they don't exist until a human completes a live consent flow against the
    customer's own Clio/QuickBooks/Estateably account post-deploy — Phase 3's onboarding
    flow, not the CFN template, is the actual secrets-provisioning mechanism. (See
    Section 6, point 4.)
12. Evaluate and, if justified by expected buyer demand, implement an optional
    "Lambda-in-VPC, no public internet egress" deployment mode (VPC + interface
    endpoints for Secrets Manager/S3/Bedrock, or a NAT gateway) as a Terraform toggle,
    for regulated buyers whose security baseline forbids public-internet-reachable
    compute — the default (no VPC) stays the out-of-the-box path. (See Section 6,
    point 5.)

---

### Phase 6 — QA: fresh-account end-to-end validation (All tracks, Days 16–19)

**Agenda:** Deploy the *entire* stack into a brand-new, empty AWS account using only the
Phase 3 onboarding path and the Phase 4 CloudFormation entry point — no engineer manually
patching anything. This is the real test of whether Phases 1–4 actually removed every
hidden Aphrodite-specific assumption. Also run: concurrent matter-search load test (fan-
out + Bedrock throttling behavior under load), a cost-estimate pass (actual AWS spend for
a realistic month of usage, for the listing's pricing page), and a rollback/uninstall test
(does deleting the CloudFormation stack actually clean up, or orphan resources like S3
buckets with deletion protection?).

**Problem it solves:** Everything before this phase is believed to work; this phase is
what proves it, under the same conditions a real buyer will experience.

**Difficult parts:** Bedrock model access/quotas are enabled per-AWS-account by the
customer (a one-time manual step in the Bedrock console) — the fresh-account test will
surface exactly how confusing that step is for a non-AWS-expert buyer, and that finding
needs to become a documented pre-requisite, not a support ticket after launch.

**Tasks:**
1. Provision a brand-new, empty AWS (sandbox) account with no prior Aphrodite history.
2. Enable Bedrock model access manually as a real buyer would, and document/time how
   clear or confusing that step is.
3. Deploy the Phase 4 CloudFormation template end-to-end with zero engineer intervention
   beyond what's documented for the buyer.
4. Run the Phase 3 onboarding flow end-to-end against real sandbox Clio/QuickBooks/Entra/
   SharePoint test accounts.
5. Exercise all four modules (Matter Search, Quarterly Report, AML Export, Sanction
   Screener), including at least one scenario with a connector disabled (Phase 2).
6. Run a concurrent load test against the Matter Search fan-out and observe Bedrock
   throttling/backoff behavior under load.
7. Run a cost-tracking pass for a simulated month of realistic usage; capture real AWS
   Cost Explorer numbers for the listing's pricing page.
8. Test the `Update` path (deploy v1, then update to v1.1) in this same account.
9. Test the `Delete`/uninstall path; confirm no orphaned resources (especially S3 buckets
   with deletion protection/Object Lock) and document any required manual cleanup.
10. File every bug/friction point found; fix blockers before Phase 7, log non-blockers to
    the post-launch backlog.
11. Verify Bedrock model availability **and default service quotas** in every AWS region
    the listing claims to support — a brand-new account's default quota can be too low
    for real usage and requires a support-ticket increase, and not every Claude model is
    available in every region (directly relevant to the `ca-central-1` data-residency
    pitch). Document the quota-increase process as a pre-deployment prerequisite. (See
    Section 6, point 10.)
12. Run at least one deployment test inside an **AWS Organizations member account with
    representative Service Control Policies applied** (not just a standalone sandbox
    account) — SCPs commonly block the IAM/OIDC role creation the bootstrap template
    depends on in ways a bare sandbox account never surfaces. (See Section 6, point 10.)
13. Model realistic Bedrock token cost per active user/month (the main cost driver in
    this architecture, in place of the NAT-Gateway/EC2/RDS costs a typical Marketplace
    product worries about) for the Phase 7 pricing page, including S3/CloudFront egress
    for large SharePoint document crawls. (See Section 6, point 9.)
14. Add optional CloudWatch billing/cost alarms, deployed via Terraform, that a customer
    can enable as a self-service cost guardrail.

---

### Phase 7 — Marketplace listing submission package (All tracks, prep from Day 14, submit Day 20)

**Agenda:** Finalize and submit everything AWS Marketplace requires that isn't code:
product title/description/category, pricing page, support plan + SLA, EULA/Terms of Use
(legal review), screenshots/demo walkthrough, the security questionnaire from Phase 5,
and seller account registration/tax/banking details if not already done. Submit for AWS's
technical + business review.

**Problem it solves:** This is literally what makes the product *visible and purchasable*
on AWS Marketplace.

**Difficult parts / timeline risk:** AWS's review turnaround is not in our control
(historically 1–6 weeks) and only starts after this phase — **the product can be
submission-ready on Day 20 without being publicly live on Day 20.** Build buffer into any
external commitment (e.g., a customer-facing launch date) accordingly.

**Tasks:**
1. Write the product title, short/long description, and category selection for the
   listing.
2. Build the pricing page content from the Phase 4 pricing model and Phase 6's real
   cost-tracking numbers.
3. Write the support plan and SLA (response times, channels, escalation path).
4. Draft the EULA/Terms of Use and route it through legal review.
5. Capture screenshots and record a short demo walkthrough of the four modules.
6. Compile the Phase 5 security questionnaire/data-flow doc into AWS Marketplace's
   requested format.
7. Confirm AWS Marketplace seller registration (tax, banking, account linking) is
   complete.
8. Assemble the full listing in the AWS Marketplace Management Portal.
9. Prepare an internal support FAQ/runbook for likely first-customer questions.
10. Submit for AWS technical + business review, and set expectations with any waiting
    pilot customer on the realistic review timeline (1–6 weeks) separately from the
    Day-20 engineering completion date.

---

## 4. Schedule: tracks × days

| Day(s) | Track 1 (Eng-1) | Track 2 (Eng-2) | Track 3/4 (Eng-3 [+Eng-4]) | Milestone |
|---|---|---|---|---|
| **1** | Phase 1: inventory hardcoded rules, design config schema | Phase 3: design onboarding sequence, scaffold Setup screen | **CFN-triggers-Terraform spike** (isolated prototype) + Eng-4: wire up CI security scanning | De-risk the one unproven assumption before committing to it |
| **2–3** | Phase 1: refactor `render_docx`/`quality.py`/`scoping.py`/`framework.py`, add tests | Phase 3: build Clio OAuth callback Lambda + refactor bootstrap logic | Decide custom-domain approach from spike results; start CFN template + CodeBuild + fold in bootstrap; Eng-4: QuickBooks OAuth callback + metering Lambda skeleton | |
| **4–6** | Phase 1 wrap-up → start Phase 2 (connector/module toggles) | Phase 3: build Setup screen UI, wire health checks, write customer guides | Build custom-resource Lambda + async polling; data-retention audit; Eng-4: finish billing/entitlement, pull forward IAM audit of bootstrap policy | |
| **7** | — | — | — | **Checkpoint 1:** Tracks 1&2 merge to main; Track 3 re-points CFN at current Terraform |
| **8–9** | Phase 2: orchestrator/synthesize skip-logic, frontend nav, integration tests | Phase 3: dry-run full onboarding against sandbox accounts, fold in fixes | `Delete` path, versioning/changelog, `Update`-path test, sandbox Create/Update/Delete | |
| **10** | — | — | — | **Checkpoint 2 / buffer:** full-team CFN + onboarding dry run together; absorbs Track 3 slippage |
| **11–13** | Phase 5: IAM audit, `checkov`/`tfsec` remediation | Phase 5: data-flow doc, security questionnaire draft | Phase 5: cross-account support role, optional-VPC mode eval; Eng-4: secrets-logging review, OAuth-secrets doc, billing sandbox test | All tracks converge — app is feature-complete |
| **14–15** | Buffer: fix security findings | Buffer: fix security findings | Buffer: fix security findings | Early finishers start Phase 7 listing-copy drafting |
| **16–17** | Phase 6: deploy fresh account, onboarding E2E | Phase 6: exercise all 4 modules incl. disabled-connector scenario | Phase 6: load test, Update/Delete test, Bedrock quota/region check, SCP-account test; Eng-4: cost-tracking, billing alarms | |
| **18–19** | Buffer: fix QA findings | Buffer: fix QA findings + listing copy | Buffer: fix QA findings | Highest-slack buffer — QA typically surfaces the ugliest surprises |
| **20** | Phase 7: final listing assembly & submission (whole team) | | | Submitted for AWS technical + business review |

**If only 3 engineers:** fold Eng-4's column into Track 3 (sequential instead of
parallel within that track) and expect Phase 4 to run closer to Days 1–13 rather than
1–10, which eats into the Day 14–15 buffer first.

---

## 5. Customer usage model (what buying and using this actually looks like)

1. **Discover & subscribe** — buyer finds the listing on AWS Marketplace, reviews pricing,
   subscribes (or receives a private offer for negotiated enterprise pricing).
2. **Deploy** — the Marketplace-triggered CloudFormation stack deploys Aphrodite into the
   buyer's own AWS account (Phase 4).
3. **Onboard** — buyer's admin runs the guided setup (Phase 3): connects Clio/
   Estateably/QuickBooks/SharePoint, configures org-specific settings (Phase 1), sets up
   Cognito SSO (optionally federated to their Entra ID).
4. **Use** — staff log in via SSO to the React SPA: ask the Matter Search agent questions,
   review quarterly report drafts, run the monthly AML export, adjudicate sanctions
   screening matches. Everything stays human-reviewed; nothing is auto-sent externally.
5. **Billing** — AWS bills the buyer directly (Marketplace handles invoicing) per the
   Phase 4 pricing model; the buyer separately pays their own AWS infrastructure costs
   (Lambda, Bedrock, S3) since it runs in their account.
6. **Support** — buyer raises issues through Loop8's support channel; Loop8 diagnoses
   using the Integrations health tab and, if needed, the scoped cross-account support
   role (Phase 5) — never broad account access.
7. **Updates** — new Aphrodite versions roll out through the same CloudFormation/Terraform
   path (Phase 4), ideally with a changelog and an opt-in "apply update" action rather than
   silent auto-upgrades to a regulated customer's production environment.

---

## 6. Cross-cutting risks & assumptions

- **Assumes** AWS Bedrock model access is available in the buyer's chosen region — not
  guaranteed for every AWS region/account tier; needs to be a documented prerequisite.
- **Assumes** the buyer is willing to run infrastructure in their own AWS account (BYOA) —
  some buyers without in-house AWS expertise may find this a harder sell than a pure
  SaaS-you-just-log-into product; the onboarding docs need to address this directly.
- **Risk:** Phase 4 (CloudFormation-triggers-Terraform) is still the least proven pattern
  in this plan. The Day 1 spike (Section 2) and the Day 10 integration checkpoint exist
  specifically to surface this risk early rather than on Day 13. If it still runs long
  after that, the buffer at Days 14–15 and 18–19 is where it gets absorbed — don't cut
  Phase 5 (security) or Phase 4 itself short to protect those dates.
- **Risk:** the parallel-track plan (Section 2) assumes Track 1 and Track 3 can agree on
  the shared `terraform/variables.tf` shape by end of Day 1 without a long design
  back-and-forth. If that takes longer, it delays both tracks simultaneously — worth a
  short joint design conversation before Day 1 work starts, not during it.
- **Out of scope for these 20 days** (explicitly deferred, not forgotten): fully zero-touch
  self-service signup (no human in the onboarding loop at all), SOC 2 / formal third-party
  penetration test, multi-region active deployments for a single customer, a hosted
  (non-BYOA) SaaS tier.

---

## 7. Architecture-specific challenge assessment

The 10-point challenge list below is the standard checklist for turning an app into an
AWS Marketplace product — and it's the right checklist for a typical **EC2/RDS/VPC**
application. Aphrodite is **fully serverless** (Lambda, API Gateway, S3, CloudFront,
DynamoDB, Cognito, EventBridge, Bedrock — no EC2, no RDS, no VPC, no load balancer, no
AMI today). That changes which of these 10 problems we actually have, and sharpens what
the *real* equivalent risk is. Going through each one against the actual codebase:

### 1. Application packaging and deployment — **partially applies, mostly already solved**
No Docker/AMI/manual-startup problem exists because there's no server to start: the
backend ships as versioned Lambda zip artifacts built reproducibly in CI
(`.github/workflows/terraform.yml` pins `manylinux2014`/`cp313` wheels precisely so the
CI runner's Python ABI never leaks into the deployed artifact), and the frontend is a
static SPA whose API endpoint/Cognito config is injected at deploy time into
`config.json` from live Terraform outputs — never hardcoded. The real residual risk is
narrower than "will it run at all": **are there any remaining assumptions, in code, that
a specific AWS account/region/resource name exists?** → covered by a new Phase 1 audit
task below.

### 2. Cross-account AWS deployment — **applies, and is the actual core of Phase 4**
Unlike a typical product that grew up pointing at one company's RDS instance and S3
buckets, Aphrodite's Terraform already provisions every resource it needs fresh, per
deployment — there was never a "move our shared DB" problem because there's no shared
DB. The genuine open risk is more subtle: confirming there is **zero** residual pointer
back to Loop8's own account (a shared Bedrock Knowledge Base ID, a shared KMS key ARN,
a hardcoded ARN left over from development) anywhere in the Lambda code or Terraform
defaults. This is exactly what Phase 6's fresh-account test (Day 17–18) is designed to
catch, but it's cheap enough to also catch earlier — added as a Phase 1 task.

### 3. "CloudFormation template complexity" — **applies, but the dependency chain is different**
No VPC/subnet/security-group/EC2/RDS chain. The real dependency chain here is: Cognito
→ API Gateway → Lambda IAM roles → Secrets Manager → S3 (versioned + Object Lock) →
CloudFront → an ACM certificate that must be minted in **us-east-1** regardless of the
deployment region. Two specific sharp edges, both already visible in the current
Terraform (`terraform/variables.tf`'s `enable_custom_domain`, `terraform/modules/web/`):
- The **custom-domain flow is explicitly two-phase today** — apply once to create the
  ACM cert, a human adds a DNS validation CNAME, then apply again to attach the
  cert/domain to CloudFront. That human-in-the-middle DNS step does not fit cleanly into
  a one-shot CloudFormation `Create`. Phase 4 needs to either ship v1 **without**
  automated custom-domain support (CloudFront default domain only, documented manual
  follow-up for a custom domain) or design the custom-resource flow to pause and resume
  across that DNS step — this is a real design decision, not just an implementation
  detail, and should be made explicitly rather than discovered mid-Phase-4.
- **CloudFront distribution creation/propagation takes 15–45 minutes.** The custom
  resource's async polling (Phase 4, task 5) must be sized for this, not just for
  `terraform apply`'s own runtime.
- Rollback risk is real but shaped differently than "VPC creation fails, cascading
  delete": a failed `apply` here is far more likely to be an IAM permission gap or a
  Secrets Manager/Cognito naming collision than infrastructure sequencing — Phase 4's
  `Create`/`Update`/`Delete` sandbox testing (task 10) should specifically include a few
  deliberately-broken runs (bad parameter, insufficient IAM) to confirm failures surface
  cleanly instead of leaving a half-applied Terraform state.

### 4. Secrets and configuration management — **applies, and is structurally harder than the generic case**
Infrastructure secrets are already handled correctly (Secrets Manager + IAM roles, no
static AWS keys). The hard part here is **not** the generic "don't hardcode a DB
password" problem — it's that the connector secrets (Clio/QuickBooks/Estateably OAuth
refresh tokens) **do not exist at stack-launch time**. They can only be created by a
human completing a live OAuth consent flow against the customer's own Clio/QuickBooks/
Estateably account, which structurally cannot be a CloudFormation `NoEcho` parameter
collected at subscribe-time. This is precisely why Phase 3 (self-service onboarding)
exists as its own phase rather than being folded into the CloudFormation parameters in
Phase 4 — worth stating explicitly so it isn't re-litigated later as "why can't the
CFN template just ask for the Clio API key."

### 5. Networking and security — **mostly doesn't apply today; one real variant to support**
There's no VPC, so there's no CIDR-conflict, NAT Gateway, or private-subnet problem as
described. The real equivalent: some regulated buyers' security policies may require
Lambda functions to run **inside a VPC with no public internet egress** (common in
bank/trust-company security baselines) even though every connector call (Clio,
Estateably, QuickBooks, Graph, Bedrock) is a public-internet HTTPS call today. Supporting
that is a genuinely different deployment mode (VPC + NAT or VPC endpoints for the AWS
services involved), not the default, and should be an explicit optional Terraform toggle
rather than assumed — added as a Phase 5 task so it's evaluated deliberately, not
discovered when a customer's security team blocks the default deployment outright.

### 6. Database migration and data persistence — **applies, with DynamoDB/S3 in place of RDS**
No SQL database, so no Alembic/schema-migration problem. But the equivalent risk is real
and currently **unverified**: the DynamoDB tables (jobs, activity log, integration
health) and the S3 outputs/reference buckets must survive a CloudFormation stack
`Delete` or a failed `Create` rollback without losing a customer's history or archived
reports. The outputs bucket already uses versioning + Object Lock per the existing
design, which is the right instinct — but this needs to be audited resource-by-resource
(including the DynamoDB tables, which currently have no stated retention/backup policy)
rather than assumed. Added as explicit Phase 4 tasks: audit every stateful resource for
retention behavior, enable DynamoDB point-in-time recovery, and decide/document what
survives a stack deletion.

### 7. Marketplace-specific compliance — **applies, already the subject of Phase 7**
One clarification worth stating plainly: because Aphrodite isn't shipping as an AMI or
container, AMI vulnerability scanning doesn't apply — the equivalent control is the
Phase 5 dependency/secrets scanning of the Lambda code and its packaged dependencies.
The delivery-model choice itself (CloudFormation-triggered, buyer's own account) is
already made; Phase 7 just needs to document it accurately in the listing.

### 8. Updates and version management — **applies, already covered, with one addition**
Already in Phase 4 (versioning/changelog, `Update` path testing) and Phase 6 (testing a
real v1→v1.1 update). One addition worth calling out: Aphrodite's "data" isn't only
database rows — it includes customer-edited **config artifacts** in S3 (the AML
designation list, the account-classification workbook). A version update must not
silently fail to read an older config file's format. Added as a Phase 4 task.

### 9. Cost optimization — **mostly a strength here, with a different cost driver to watch**
No NAT Gateway, no multi-AZ RDS, no always-on EC2, no load balancer — the serverless
architecture avoids most of the classic Marketplace cost-bloat list by construction, and
that's worth stating as a selling point in the Phase 7 listing, not just a risk to
mitigate. The real cost driver here is different: **Bedrock LLM token usage** scaling
with matter-search volume, and S3/CloudFront egress for large SharePoint document
crawls. Added as a Phase 6 task: model realistic Bedrock cost per active user/month for
the pricing page, and ship optional CloudWatch billing alarms as a customer-enabled
guardrail.

### 10. Testing across different customer environments — **applies, and needs to go further than one sandbox account**
This is a real gap in the plan as written. Phase 6 currently tests in *one* fresh sandbox
account — that's necessary but not sufficient. Two risks specific to Aphrodite's stack
that a single clean sandbox account won't surface: **Bedrock model availability and
default quotas vary by region and by account** (a brand-new account's default Bedrock
quota can be too low for real usage and requires a support-ticket increase — and not
every Claude model is available in every region, which matters a lot given the whole
pitch is `ca-central-1` data residency), and **AWS Organizations member accounts
commonly carry Service Control Policies** that can silently block the IAM/OIDC role
creation the bootstrap template depends on, in ways a standalone sandbox account never
would. Added as explicit Phase 6 tasks: verify Bedrock model/quota availability per
supported region as a documented prerequisite, and run at least one deployment test
inside an AWS Organizations member account with representative SCPs applied.
