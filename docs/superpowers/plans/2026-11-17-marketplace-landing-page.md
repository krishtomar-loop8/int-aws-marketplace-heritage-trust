# Marketplace Landing Page & Launch-Link Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A buyer who subscribes to Aphrodite on AWS Marketplace lands on a
page Loop8 controls, which confirms the subscription is real, collects a
company name and admin email, records the subscription for Loop8's own
tracking, and hands the buyer a CloudFormation launch link pre-filled with
that information — all living in Loop8's own AWS account, entirely separate
from the product this installer deploys into the buyer's account.

**Architecture:** A new top-level `loop8-landing/` directory holds
everything for this project, kept deliberately separate from the
customer-product directories (`backend/`, `frontend/`, `terraform/`,
`installer/`) since it deploys into a different AWS account and has nothing
to do with the deployed product's own runtime. A one-time CFN bootstrap
(mirroring `terraform/bootstrap/bootstrap.yaml`'s existing pattern, but for
Loop8's account) creates a Terraform state backend + GitHub OIDC role; a
Terraform root then provisions a DynamoDB table (two record shapes: a
short-lived pending-resolution record and a durable subscriber record), an
S3+CloudFront static site, an API Gateway + one Lambda (two routes, mirroring
the existing product's one-Lambda-dispatches-by-path convention in
`backend/api/handlers.py`), Route 53 + ACM (Loop8 already controls the
`loop8.io` zone in this account, so DNS validation is fully automated), and
an S3 bucket that mirrors `installer.yaml` releases. A new GitHub Actions
workflow (its own OIDC trust; `release.yml` is untouched and stays
credential-free) deploys this stack and, triggered after `release.yml`
completes, mirrors the release's `installer.yaml` into that bucket. Finally,
`installer.yaml` gains two parameters (`CompanyName`, `AdminEmail`) that
plug into the *existing* business-config system exactly the way `org_name`
already does — no new config mechanism, just a 14th field following the
same pattern.

**Tech Stack:** Python 3.13 (Lambda), Terraform, AWS CloudFormation (the
one-time bootstrap only — matching the existing bootstrap pattern), plain
HTML/CSS/vanilla JS for the one-page form (no React/Vite build pipeline for
a single static page — the existing product's SPA toolchain would be pure
overhead here), GitHub Actions with OIDC.

**Spec:** `docs/superpowers/specs/2026-11-17-marketplace-landing-page-design.md`

## Global Constraints

- Everything in this plan lives under a new top-level `loop8-landing/`
  directory (bootstrap CFN, Terraform, Lambda code, the static form page) —
  never inside `backend/`, `frontend/`, `terraform/`, or `installer/`, which
  are entirely about the customer-deployed product.
- The Marketplace token is received only as a POST body, never a URL query
  parameter or header, and the API's own opaque session ID (issued after
  resolving that token) is the only thing that travels in a URL — never the
  real Marketplace token itself (spec Anti-goals).
- `.github/workflows/release.yml` is not modified at all — the new workflow
  this plan adds has its own trigger and its own OIDC trust into Loop8's
  account (spec Constraints).
- `installer/installer.yaml`'s existing five parameters
  (`AphroditeVersion`, `BundleSha256`, `Environment`, `SamlMetadataUrl`,
  `DeleteData`) are unchanged; `CompanyName`/`AdminEmail` are additive.
- `backend/api/config_admin.py`'s module docstring states its 13 fields
  "mirror the existing Terraform variables 1:1" — the new `admin_email`
  field (making it 14) must follow that exact same pattern: a Terraform
  variable, an env-var-sourced default, no extra validation beyond what its
  siblings already have (confirmed: only `org_name`/`org_office_address`
  enforce non-blank; no field has format validation — `admin_email` gets
  neither, matching e.g. `quarterly_exclusion_field`'s plain-string
  treatment).
- Product-code pinning (spec, "Resolved during builder-check") must not
  block testing before the Marketplace listing exists: the expected product
  code is a Terraform variable defaulting to empty, in which case the check
  is skipped (logged, not enforced) rather than failing every request.

## Review Focus

- A GET (or any non-POST) request to the fulfillment endpoint, or a POST
  with a missing/malformed `x-amzn-marketplace-token` field, must return a
  clear 4xx — not a 500, and not silently redirect to the form as if
  resolution had succeeded.
- A submit request whose session ID is missing, already used, or expired
  must be rejected with a specific reason the frontend can show as an error
  state — never silently treated as a fresh, valid session.
- The generated CloudFormation quick-create URL must correctly
  URL-encode the company name and admin email (both free-text fields a
  buyer could fill with spaces, `&`, or other URL-significant characters)
  — a malformed query string would corrupt every parameter after it in the
  link, not just the one with the odd character.
- A Terraform `apply` of this stack from a clean state (no existing
  DynamoDB table, no existing CloudFront distribution) must succeed in one
  pass — ACM validation and CloudFront propagation have their own timing
  characteristics distinct from the product's own Terraform, worth a real
  check, not an assumption that "it's just like the other Terraform."
- `installer.yaml`'s existing CFN parameters, and every existing
  `TF_VAR_*`/business-config field, must keep working exactly as today —
  this plan is strictly additive to a working, already-shipped template and
  config schema.

---

### Task 1: Loop8-account bootstrap (CFN)

**Files:**
- Create: `loop8-landing/bootstrap/bootstrap.yaml`
- Create: `loop8-landing/bootstrap/README.md`

**Interfaces:**
- Consumes: nothing — this is the first thing deployed into Loop8's account
  for this project.
- Produces: a Terraform state S3 bucket and a GitHub OIDC IAM role (ARN)
  that Task 2's Terraform and Task 5's GitHub Actions workflow both need.
  Name these outputs exactly `StateBucketName` and `DeployRoleArn` in the
  CFN template's `Outputs` block — Task 5's workflow file references them
  by these exact names in its setup instructions.

Read `terraform/bootstrap/bootstrap.yaml` in full first — this task is a
new, analogous template for a *different* AWS account, not a reference to
or modification of that file. Copy its structure (OIDC provider + role +
state bucket with versioning/encryption/public-access-block), scoped to:
- The OIDC role's trust policy restricted to this repo's GitHub Actions
  (`repo:krishtomar-loop8/int-aws-marketplace-heritage-trust:*`, matching
  the existing bootstrap's trust-condition style) and to the new workflow
  file's path specifically if the existing template scopes by workflow path
  — check and mirror whatever specificity the existing one uses.
- The deploy role's permissions scoped to exactly what Task 2's Terraform
  needs to create (DynamoDB, S3, CloudFront, API Gateway, Lambda, IAM
  roles for the Lambda, Route 53, ACM, and Terraform-state S3 access) — do
  not grant broad `*` permissions; enumerate the actions this stack's
  resources actually require.

- [ ] **Step 1: Write `loop8-landing/bootstrap/bootstrap.yaml`**

Mirror `terraform/bootstrap/bootstrap.yaml`'s resource shapes (OIDC
provider, IAM role, S3 bucket with the same security properties: versioning
enabled, SSE-S3 encryption, all four public-access-block flags true, bucket
owner enforced). Parameterize the GitHub repo/org the same way the existing
bootstrap does (read it to find the exact parameter name and reuse it).

- [ ] **Step 2: Validate the template locally**

Run: `aws cloudformation validate-template --template-body file://loop8-landing/bootstrap/bootstrap.yaml` (or `cfn-lint loop8-landing/bootstrap/bootstrap.yaml` if available — check which this repo's existing CI uses for `installer/installer.yaml` and use the same tool).
Expected: no errors.

- [ ] **Step 3: Write `loop8-landing/bootstrap/README.md`**

A short operational runbook (one page): what this template does, that it's
a one-time manual deploy (the buyer-side bootstrap's own README is the
model — read it for tone/structure), and the exact `aws cloudformation
deploy` command to run it, naming the two required outputs
(`StateBucketName`, `DeployRoleArn`) and where they get used next (Task 2's
Terraform backend config, Task 5's workflow).

- [ ] **Step 4: Commit**

```bash
git add loop8-landing/bootstrap/
git commit -m "feat: add the one-time Loop8-account bootstrap (state backend + OIDC role)"
```

---

### Task 2: Terraform infrastructure for the landing stack

**Files:**
- Create: `loop8-landing/terraform/main.tf`
- Create: `loop8-landing/terraform/variables.tf`
- Create: `loop8-landing/terraform/outputs.tf`
- Create: `loop8-landing/terraform/dynamodb.tf`
- Create: `loop8-landing/terraform/site.tf` (S3 + CloudFront + Route 53 + ACM)
- Create: `loop8-landing/terraform/api.tf` (API Gateway + Lambda resource +
  IAM role for the Lambda)

**Interfaces:**
- Consumes: Task 1's `StateBucketName`/`DeployRoleArn` (used in the backend
  config and the GitHub Actions workflow, not inside the `.tf` files
  themselves — the backend block's bucket name is a literal value a human
  fills in once, matching how the product's own `terraform init` is
  parameterized per the existing deploy script's pattern — check
  `installer/deploy.sh`'s `tf_init()` for the exact style, a
  partial-backend-config approach, and mirror it).
- Produces: a DynamoDB table name (Terraform output `subscriptions_table`),
  an API Gateway invoke URL (output `api_url`), and a CloudFront domain
  (output `site_url`) — Task 3's Lambda code needs the table name (as an
  env var, wired in `api.tf`'s Lambda resource, not read from this output
  directly), and Task 5's workflow needs `api_url`/`site_url` to confirm a
  successful deploy.

- [ ] **Step 1: DynamoDB table**

One table, `loop8-aphrodite-landing-subscriptions` (or let Terraform name
it via a `name_prefix` — your call, follow whatever naming convention
`terraform/modules/*/main.tf`'s existing DynamoDB tables in this repo use),
partition key `pk` (String) only — no sort key, no GSI (the spec's own "no
complex query needs yet" call). Two record shapes share this one table,
distinguished by `pk`'s prefix:
- Pending-resolution records: `pk = "SESSION#<uuid>"`, attributes for the
  resolved `CustomerAWSAccountId`/`ProductCode`/`CustomerIdentifier`, a
  `used` boolean (default false), and a DynamoDB TTL attribute (`expires_at`,
  epoch seconds, ~30 minutes out) so AWS auto-deletes unused sessions.
- Subscriber records: `pk = "SUB#<uuid>"`, attributes for company name,
  admin email, the resolved customer/product identifiers, and a
  `created_at` timestamp — no TTL (durable).

Enable point-in-time recovery (matching the readiness plan's existing
guidance for the product's own tables) and SSE (DynamoDB's default
encryption is sufficient — no customer-managed KMS key needed for this
lead-tracking data, per the spec's Constraints noting this isn't subject to
the product's own data-handling rules).

- [ ] **Step 2: Static site (S3 + CloudFront + Route 53 + ACM)**

An S3 bucket (private, CloudFront-only access via OAC — mirror the existing
product's `web_bucket` module's security properties, read
`terraform/modules/*/main.tf` for whichever module provisions the
product's own CloudFront+S3 site and copy its OAC/bucket-policy pattern
rather than inventing a new one). An ACM certificate in `us-east-1` for
`deploy.loop8.io`, DNS-validated via a Route 53 record in the
already-existing `loop8.io` hosted zone in this account (a
`data.aws_route53_zone` lookup, not a new zone resource). A CloudFront
distribution serving the S3 bucket at `deploy.loop8.io`, plus an
`aws_route53_record` (alias) pointing that name at the distribution.

- [ ] **Step 3: API Gateway + Lambda resource + the installer-mirror bucket**

An HTTP API Gateway (matching the product's own API Gateway type — confirm
HTTP API vs REST API by checking `terraform/modules/search_api/main.tf`)
with two routes: `POST /fulfillment` and `POST /subscribe`, both pointing
at one Lambda function (Task 3 writes its code; this task declares the
`aws_lambda_function` resource, pointing its `filename`/`s3_key` at a zip
Task 5's workflow will build and upload — use a placeholder/empty zip for
now so `terraform apply` succeeds before Task 3 exists, matching how this
repo's own Lambda modules are structured: infra declared ahead of, and
independently testable from, the code that fills it). Custom domain
`api.deploy.loop8.io` for the API Gateway, with its own ACM cert (can share
the `us-east-1` cert from Step 2 if CloudFront and the regional API Gateway
custom domain can use the same cert — check; if not, a second cert in the
API Gateway's own region, DNS-validated the same way). An IAM role for the
Lambda scoped to: DynamoDB read/write on this task's table only, no other
permissions (the Lambda doesn't need `aws-marketplace:ResolveCustomer`
permission scoped narrower than the action itself, since that API has no
resource-level permissions — grant the action, not a resource ARN).

A separate S3 bucket (`loop8-aphrodite-installer-mirror` or similar) that
Task 5's workflow copies `installer.yaml` into after each release — private,
but with a bucket policy allowing public `s3:GetObject` on just the
installer-template object path (CloudFormation's quick-create console needs
to fetch `templateURL` over plain HTTPS with no auth) — do not make the
whole bucket public, only the specific object path pattern this template
lives at.

- [ ] **Step 4: Terraform validate**

Run: `terraform init -backend=false && terraform validate` (from
`loop8-landing/terraform/`) — matching this repo's own established
precedent that this is a purely local, read-only syntax check (CLAUDE.md's
"no local deployment" is about not running real `apply`, not about
avoiding this).
Expected: Success.

- [ ] **Step 5: `terraform fmt -check`**

Run: `terraform fmt -check -recursive loop8-landing/terraform/`
Expected: no output (already formatted) or fix with `terraform fmt
-recursive` and re-check.

- [ ] **Step 6: Commit**

```bash
git add loop8-landing/terraform/
git commit -m "feat: add Terraform infrastructure for the Marketplace landing stack"
```

---

### Task 3: Backend Lambda — token resolution, session issuance, subscription

**Files:**
- Create: `loop8-landing/backend/handler.py`
- Create: `loop8-landing/backend/tests/test_handler.py`

**Interfaces:**
- Consumes: Task 2's DynamoDB table (its name arrives as an env var,
  `SUBSCRIPTIONS_TABLE`, matching this repo's existing convention of
  passing resource identifiers as Lambda env vars rather than hardcoding
  them).
- Produces: `handler(event, context) -> dict` (API Gateway Lambda proxy
  response shape, matching `backend/api/handlers.py`'s own
  `_response(status, body)` convention — reuse that exact
  `{"statusCode": ..., "headers": {"Content-Type": "application/json"},
  "body": json.dumps(body)}` shape, don't invent a different one). Routes
  dispatched by `event["requestContext"]["http"]["method"]`/`["path"]`,
  mirroring `backend/api/handlers.py`'s own dispatch style exactly (this
  repo already has one established pattern for this; use it, don't invent
  a second one).

This is new, standalone code with no existing test suite to extend — write
it test-first using `boto3`'s `stubber` or a lightweight fake DynamoDB
client (check whether `backend/api/tests/`'s existing `FakeDynamo` test
double, used by `backend/api/tests/test_jobs.py`, can be reused here by
importing it, or whether this new, separate top-level directory needs its
own minimal copy — prefer importing the existing one if `loop8-landing/`'s
test setup can reach it without creating a cross-directory dependency that
complicates this project's independence from the main product; if it
can't cleanly, write a small local fake rather than fighting the import
path).

- [ ] **Step 1: Write the failing tests**

```python
# loop8-landing/backend/tests/test_handler.py

def test_fulfillment_rejects_non_post():
    result = handler(http_event("GET", "/fulfillment"), None)
    assert result["statusCode"] in (400, 405)

def test_fulfillment_rejects_missing_token():
    result = handler(http_event("POST", "/fulfillment", body=""), None)
    assert result["statusCode"] == 400

def test_fulfillment_resolves_token_and_redirects_with_opaque_session(monkeypatch):
    # Stub ResolveCustomer to return a fixed CustomerIdentifier/CustomerAWSAccountId/ProductCode.
    # Assert: statusCode 302, Location header is https://deploy.loop8.io/setup?session=<uuid>,
    # the uuid is NOT the marketplace token itself, and a pending-resolution item with that
    # session id now exists in the fake table with used=False and an expires_at in the future.
    ...

def test_fulfillment_rejects_product_code_mismatch_when_pinning_is_configured(monkeypatch):
    # With EXPECTED_PRODUCT_CODE env var set, a ResolveCustomer response for a different
    # product code must be rejected (4xx), not redirected.
    ...

def test_fulfillment_skips_product_code_check_when_pinning_is_unconfigured(monkeypatch):
    # With EXPECTED_PRODUCT_CODE unset/empty, any resolved product code is accepted —
    # testable before the real Marketplace listing (and its real product code) exists.
    ...

def test_subscribe_rejects_missing_session():
    result = handler(http_event("POST", "/subscribe", body='{"company_name": "Acme", "admin_email": "a@acme.com"}'), None)
    assert result["statusCode"] == 400

def test_subscribe_rejects_unknown_or_expired_session():
    ...

def test_subscribe_rejects_already_used_session():
    ...

def test_subscribe_writes_subscriber_record_and_marks_session_used():
    # Seed a valid pending-resolution item, submit valid company_name/admin_email,
    # assert: a SUB# record now exists with those values + the resolved identifiers,
    # the SESSION# record's used flag is now True, statusCode 200.
    ...

def test_subscribe_returns_a_correctly_encoded_launch_url():
    # company_name containing a space and an "&" — assert the returned launch_url's
    # param_CompanyName query value, when URL-decoded, equals the original string exactly,
    # and that the "&" didn't corrupt the admin_email param that follows it.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest loop8-landing/backend/tests/test_handler.py -v`
Expected: FAIL (`handler` not defined).

- [ ] **Step 3: Implement `loop8-landing/backend/handler.py`**

Two route handlers, dispatched the same way `backend/api/handlers.py`
dispatches:

`_handle_fulfillment(event)`: parse the POST body (form-encoded,
`x-amzn-marketplace-token=...` — AWS Marketplace sends this as
`application/x-www-form-urlencoded`, not JSON; use `urllib.parse.parse_qs`
on the raw body, not `json.loads`). If the token is missing, return 400.
Call `boto3.client("meteringmarketplace").resolve_customer(RegistrationToken=token)`
(the boto3 API name for `ResolveCustomer` — confirm the exact client/method
name against boto3's docs before writing the call, don't guess). If
`EXPECTED_PRODUCT_CODE` env var is set and non-empty and doesn't match the
response's `ProductCode`, return 403. Otherwise generate a UUID session id,
write a pending-resolution item (`pk=f"SESSION#{session_id}"`, the resolved
identifiers, `used=False`, `expires_at` = now + 1800s), and return a 302
response with `Location: https://deploy.loop8.io/setup?session={session_id}`.

`_handle_subscribe(event)`: parse the JSON body for `session_id`,
`company_name`, `admin_email`. Look up `pk=f"SESSION#{session_id}"`; if
missing, expired (compare `expires_at` to now — DynamoDB TTL deletion isn't
instantaneous, so check this explicitly rather than relying solely on TTL
having already removed it), or `used=True`, return 400 with a reason string
the frontend can show. Otherwise: write a subscriber item (`pk=f"SUB#{uuid4()}"`,
company_name, admin_email, the pending record's resolved identifiers,
`created_at`), update the pending item's `used` to `True`, build the launch
URL, and return 200 with `{"launch_url": ...}`.

**The launch URL's exact format** (AWS's own CloudFormation quick-create
convention — get this precise, it's not a general-purpose query string):

```
https://console.aws.amazon.com/cloudformation/home?region={REGION}#/stacks/create/review?templateURL={ENCODED_TEMPLATE_URL}&stackName={STACK_NAME}&param_CompanyName={ENCODED_COMPANY}&param_AdminEmail={ENCODED_EMAIL}
```

Everything after the `#` is itself a second, independent query string (a
CloudFormation-console-specific convention: the fragment holds the
console's own routing path *and* a nested `?...` query of its own) — build
it as its own `urllib.parse.urlencode({"templateURL": ..., "stackName":
..., "param_CompanyName": company_name, "param_AdminEmail": admin_email})`
call, then append that whole encoded string after `#/stacks/create/review?`.
Each CFN parameter becomes `param_<ExactParameterName>` — `param_CompanyName`
and `param_AdminEmail`, matching Task 6's exact CFN parameter names
character-for-character (cross-check once Task 6 exists; if either name
ever changes there, this literal string must change here too). Do not
hand-roll string concatenation for either the outer or the inner query
string — this is exactly what Review Focus's encoding requirement is
about, and the inner one is the one a buyer's own special characters in
`company_name` could corrupt.

Keep `REGION`, the installer-mirror bucket's `templateURL`, and the
default `STACK_NAME` as small, named constants or env vars at the top of
the file — not magic strings buried inside the function body.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest loop8-landing/backend/tests/test_handler.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add loop8-landing/backend/
git commit -m "feat: add the fulfillment/subscribe Lambda for the landing pipeline"
```

---

### Task 4: Frontend — the subscribe form page

**Files:**
- Create: `loop8-landing/frontend/index.html`
- Create: `loop8-landing/frontend/setup.html`
- Create: `loop8-landing/frontend/app.js`
- Create: `loop8-landing/frontend/styles.css`

**Interfaces:**
- Consumes: Task 3's `POST /subscribe` (`api.deploy.loop8.io/subscribe`),
  sending `{session_id, company_name, admin_email}` as JSON, reading back
  `{launch_url}` on success or `{error: "<reason>"}` on a 400.
- Produces: nothing consumed by a later task — last task whose output
  another task depends on (Task 5 deploys this as static files; Task 5
  doesn't read anything *from* this task's code, just uploads it).

Plain HTML/CSS/vanilla JS — no build step, no framework, no bundler. A
single page (`index.html`) can serve as a simple "you've been redirected
here after subscribing" landing/fallback (in case someone navigates here
directly without a session), and `setup.html` is what
`https://deploy.loop8.io/setup?session=...` actually serves (static S3
sites + CloudFront can route any path to the same file easily, or the two
can literally be the same file with JS checking `location.search` — your
call, keep it simple).

- [ ] **Step 1: Build the form page**

`setup.html`: a form with `company_name` and `admin_email` inputs (basic
HTML5 `required`/`type="email"` validation — this is the client-side form
validation the design doc's "What's left to the builder" section asked
for; keep it to what HTML5 already gives you, no custom JS validation
library). On submit, `app.js` reads `session` from
`new URLSearchParams(location.search)`, POSTs to the subscribe endpoint,
and on success replaces the form with the returned `launch_url` rendered
as a visible, clickable link (not an auto-redirect — the buyer should see
and click it deliberately, since it's about to open the AWS CloudFormation
console in a different context). On a 400/error response, show the
`error` reason in a plain, visible message instead of the form (no retry
loop, no complex state machine — a reasonable person just tries again by
reloading the Marketplace flow from the start if this page errors, which
is an acceptable UX for a flow that AWS itself, not this page, initiates).

- [ ] **Step 2: Style minimally**

A simple, legible `styles.css` — reuse Aphrodite's existing visual
language loosely if it helps (the forest-green palette mentioned
elsewhere in this project is a reasonable nod, not a hard requirement
since this is Loop8's own branding context, not the product's) but don't
over-invest in design polish for a single utilitarian form page.

- [ ] **Step 3: Manual verification**

Open `setup.html` directly in a browser with a `?session=test-123` query
param (no real backend needed for this check — stub `fetch` in the
browser console, or point `app.js`'s API base at a local mock temporarily)
and confirm: the form renders, submitting shows either the launch-link
success state or an error state depending on what the stubbed response
returns, and an empty/invalid email is caught by the browser's own HTML5
validation before any network call happens.

- [ ] **Step 4: Commit**

```bash
git add loop8-landing/frontend/
git commit -m "feat: add the subscribe form page"
```

---

### Task 5: CI/CD — deploy workflow + installer-mirror workflow

**Files:**
- Create: `.github/workflows/loop8-landing-deploy.yml`
- Create: `.github/workflows/loop8-landing-mirror-installer.yml`

**Interfaces:**
- Consumes: Task 1's `DeployRoleArn`/`StateBucketName` (as GitHub Actions
  secrets or repo variables — set manually once, the same way this repo's
  existing OIDC-based workflows are configured; check
  `.github/workflows/terraform.yml` or `release.yml` for the exact
  existing `permissions:`/`aws-actions/configure-aws-credentials` pattern
  and mirror it with Loop8's account's role ARN instead of the product's).
- Produces: nothing consumed by a later task — last task in this plan.

- [ ] **Step 1: `loop8-landing-deploy.yml`**

Triggered on push to `main` touching `loop8-landing/terraform/**` or
`loop8-landing/backend/**` (path filter), plus `workflow_dispatch`. Steps:
checkout, configure AWS credentials via OIDC against Loop8's
`DeployRoleArn` (mirror the exact `permissions: id-token: write` +
`aws-actions/configure-aws-credentials` block from an existing workflow in
this repo — don't write OIDC wiring from scratch when a working example
already exists here), zip `loop8-landing/backend/handler.py` into a
deployable package, upload it to S3 or pass it directly to
`terraform apply` (match whichever existing pattern this repo's Lambda
modules use — check if any existing Terraform module in `terraform/`
already does a "zip a Python file and reference it" pattern and reuse that
exact approach), then `terraform init` (with the backend config pointing
at Task 1's state bucket) and `terraform apply -auto-approve` in
`loop8-landing/terraform/`.

- [ ] **Step 2: `loop8-landing-mirror-installer.yml`**

Triggered via `workflow_run: workflows: ["Release"] types: [completed]`
(confirm `"Release"` is the exact `name:` this repo's `release.yml`
declares — read it to get the exact string) with a condition that only
proceeds `if: github.event.workflow_run.conclusion == 'success'`. Steps:
checkout, configure AWS credentials via OIDC against Loop8's
`DeployRoleArn`, download the `installer.yaml` asset from the GitHub
Release that triggered the completed `release.yml` run (use the `gh`
CLI's `gh release download` against the tag, or the GitHub API — whichever
this repo's existing workflows already use elsewhere for release assets),
and `aws s3 cp` it to the installer-mirror bucket Task 2 created, at a
versioned key (e.g. `installer/<version>/installer.yaml` and also a
`installer/latest/installer.yaml` copy the landing page's Lambda actually
points at — confirm this exact key convention matches whatever Task 3's
`handler.py` constants assume, and update one side if they don't already
agree).

- [ ] **Step 3: Verify both workflows parse**

Run: `actionlint .github/workflows/loop8-landing-deploy.yml
.github/workflows/loop8-landing-mirror-installer.yml` if `actionlint` is
available in this environment/CI; otherwise confirm valid YAML via
`python -c "import yaml; yaml.safe_load(open('...'))"` for each file.
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/loop8-landing-deploy.yml .github/workflows/loop8-landing-mirror-installer.yml
git commit -m "feat: add CI/CD for the Loop8 landing stack and installer mirroring"
```

---

### Task 6: `installer.yaml` + business-config wiring

**Files:**
- Modify: `installer/installer.yaml`
- Modify: `installer/function/handler.py`
- Modify: `terraform/variables.tf`
- Modify: `terraform/main.tf`
- Modify: `terraform/modules/search_api/variables.tf`
- Modify: `terraform/modules/search_api/main.tf`
- Modify: `backend/api/config_admin.py`
- Modify: `backend/api/tests/test_config_admin.py` (or wherever its tests
  live — confirm the exact file name)
- Modify: `frontend/src/components/DeploymentConfigPanel.tsx`

**Interfaces:**
- Consumes: nothing from Tasks 1-5 — this task is independent of the new
  `loop8-landing/` infrastructure; it only needs the *existence* of the
  `CompanyName`/`AdminEmail` concept from the design doc, not any of this
  plan's other tasks' code. Can be reviewed and merged independently of
  Tasks 1-5 if useful, though it ships together with them in this plan.
- Produces: `installer.yaml`'s two new parameters, consumed by Task 3's
  Lambda when it builds the launch URL (the parameter names
  `param_CompanyName=`/`param_AdminEmail=` in that URL must exactly match
  these CFN parameter names — cross-check Task 3's implementation against
  this task's exact parameter names once both exist).

- [ ] **Step 1: Add `CompanyName`/`AdminEmail` to `installer.yaml`**

Add to the `Parameters` block (after `SamlMetadataUrl`, matching that
parameter's style — optional, default `""`, a one-line `Description`):

```yaml
  CompanyName:
    Type: String
    Default: ""
    Description: >-
      The buyer's company name, pre-filled from the Marketplace subscribe flow.
      Seeds the deployment's organization display name (editable later in the
      Deployment Config tab).
  AdminEmail:
    Type: String
    Default: ""
    Description: >-
      The buyer's admin contact email, pre-filled from the Marketplace subscribe
      flow. Seeds the deployment's admin-email business-config field (editable
      later in the Deployment Config tab).
```

- [ ] **Step 2: Wire the two new CFN parameters to `TF_VAR_*`**

In `installer/function/handler.py`'s `env` dict (the one containing
`"TF_VAR_saml_metadata_url": src.get("SamlMetadataUrl", "")`), add:

```python
"TF_VAR_org_name": src.get("CompanyName", ""),
"TF_VAR_admin_email": src.get("AdminEmail", ""),
```

`CompanyName` maps directly to the *existing* `org_name` Terraform
variable (no new Terraform variable for company name — it already exists
and already flows through to the business-config `org_name` field exactly
the way this needs). Only `admin_email` is a genuinely new Terraform
variable (Step 3).

- [ ] **Step 3: Add the `admin_email` Terraform variable, mirroring `org_name`'s exact wiring**

In `terraform/variables.tf`, immediately after the existing `org_name`
variable:

```hcl
variable "admin_email" {
  type        = string
  default     = ""
  description = "Deployment's admin contact email, set at first launch from the Marketplace subscribe flow; editable later via the Deployment Config tab."
}
```

In `terraform/main.tf`, add `admin_email = var.admin_email` next to the
existing `org_name = var.org_name` line (same module block). In
`terraform/modules/search_api/variables.tf`, add the matching
`variable "admin_email"` block (identical shape to that file's own
existing `org_name` variable). In `terraform/modules/search_api/main.tf`,
add `APHRODITE_ADMIN_EMAIL = var.admin_email` next to each existing
`APHRODITE_ORG_NAME = var.org_name` line (there are two — confirmed by the
earlier grep of this file; add the new env var next to both).

- [ ] **Step 4: Write the failing test for the new business-config field**

```python
# backend/api/tests/test_config_admin.py (confirm exact existing filename)

def test_admin_email_defaults_from_env_var(monkeypatch):
    monkeypatch.setenv("APHRODITE_ADMIN_EMAIL", "ops@example.com")
    result = get_config(fake_config_client_with_no_stored_values())
    assert result["fields"]["admin_email"]["value"] == "ops@example.com"
    assert result["fields"]["admin_email"]["source"] == "default"  # matches org_name's existing source labeling

def test_admin_email_can_be_blank_unlike_org_name():
    # admin_email is NOT in _NON_BLANK_STRING_FIELDS — an empty string must be
    # accepted by put_config, unlike org_name/org_office_address.
    put_config(fake_config_client(), {"admin_email": ""})  # must not raise
```

(Match these two tests' exact style/fixtures to whatever this file's
existing `org_name`-related tests already use — read 2-3 of them first.)

- [ ] **Step 5: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_config_admin.py -k admin_email -v`
Expected: FAIL (`admin_email` not a recognized field).

- [ ] **Step 6: Add the `admin_email` field to `config_admin.py`**

In `_FIELDS`, after the `org_name` entry:

```python
_Field("admin_email", str, "APHRODITE_ADMIN_EMAIL", ""),
```

Do not add `"admin_email"` to `_NON_BLANK_STRING_FIELDS` — it is optional,
matching fields like `quarterly_exclusion_field`, not `org_name`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_config_admin.py -v`
Expected: PASS, all tests (existing ones unedited, plus this task's two
new ones).

- [ ] **Step 8: Surface the field in the Deployment Config UI**

In `frontend/src/components/DeploymentConfigPanel.tsx`'s `SECTIONS` array,
add `{ name: "admin_email", label: "Admin email" }` in the same section
`org_name` lives in (read the surrounding entries to place it sensibly —
likely right next to `org_name`).

- [ ] **Step 9: Typecheck and build the frontend**

Run: `npx tsc -b && npm run build` (in `frontend/`).
Expected: both clean.

- [ ] **Step 10: Confirm the full backend suite still passes**

Run: `python -m pytest backend/ services/ -q`
Expected: PASS, no regressions.

- [ ] **Step 11: Commit**

```bash
git add installer/installer.yaml installer/function/handler.py terraform/variables.tf terraform/main.tf terraform/modules/search_api/variables.tf terraform/modules/search_api/main.tf backend/api/config_admin.py backend/api/tests/test_config_admin.py frontend/src/components/DeploymentConfigPanel.tsx
git commit -m "feat: add CompanyName/AdminEmail installer parameters and the admin_email business-config field"
```
