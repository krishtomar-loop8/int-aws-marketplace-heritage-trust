# AWS Marketplace landing page & launch-link pipeline

## Intent and the why

Today, a buyer who subscribes to Aphrodite on AWS Marketplace has no path from "I subscribed" to
"my deployment is running" — the actual deployment mechanism (`installer/installer.yaml`, a
CloudFormation template the buyer launches themselves, which drives a CodeBuild/Terraform apply
into *their own* AWS account) exists and works, but nothing connects a Marketplace subscription to
it yet.

AWS Marketplace supports a zero-code path here (a direct "Launch CloudFormation" button, no
seller-hosted page needed at all), but Loop8 wants more: a page it controls that collects the
buyer's company name and admin email before handing them the launch link, and keeps a record of
every subscriber for its own sales/support tracking. This project builds that page and its
backend — a new, small piece of infrastructure that lives in **Loop8's own AWS account**, not the
buyer's, since it runs before any buyer-side deployment exists.

## Goals

- A buyer who clicks "Subscribe" on the Marketplace listing lands on `deploy.loop8.io`, which
  confirms their subscription is real (not a forged/replayed link) before showing them anything.
- The page collects company name and admin email, then gives the buyer a CloudFormation "Launch
  Stack" link pre-filled with those two values as stack parameters — one click starts their
  deployment, exactly like a direct Marketplace launch would, just with that information already
  in place.
- Loop8 keeps a durable record of every subscription that reaches this page (company, admin
  email, the subscribing AWS account's identifier, product code, timestamp) for its own tracking
  — this is the whole reason Option B (a custom page) was chosen over AWS Marketplace's built-in,
  code-free launch button.
- `installer/installer.yaml` gains the two new parameters (`CompanyName`, `AdminEmail`) this page
  needs to pre-fill — a small, additive change to an existing, working template.

## Non-goals

- This project does not change how the buyer's own deployment works once they click Launch — the
  existing CloudFormation → CodeBuild → Terraform mechanism in the buyer's account is untouched.
- This project does not implement the billing/entitlement decision already tracked separately in
  `MARKETPLACE_READINESS_PLAN.md` (flat vs. usage-based pricing, `ResolveCustomer`/`GetEntitlements`
  checked *inside* the deployed app). That decision is independent of this page and stays exactly
  where the existing plan puts it.
- This project does not build a marketing site, pricing page, or anything beyond the one
  subscribe → collect → launch-link flow. If Loop8 wants a fuller marketing presence at
  `loop8.io` later, that's a separate project.
- This is not "automated deployment" in the sense of Loop8's side doing anything to the buyer's
  AWS account — the buyer still clicks the link and runs the CloudFormation stack themselves, in
  their own account, under their own credentials. Loop8's page never touches buyer infrastructure.

## Anti-goals (would make this a failure even if it "works")

- Trusting the Marketplace token without actually resolving it against AWS's own API. The token
  received in the redirect must be validated server-side (`aws-marketplace:ResolveCustomer`) —
  anything that skips this and trusts the token's face value, or validates it client-side only,
  is a security hole (a forged or replayed token could fabricate a fake subscription record).
- Sending the Marketplace token, or anything sensitive, as a URL query parameter or HTTP header
  anywhere in this flow. AWS Marketplace itself delivers the token via a POST body (a browser
  form auto-submit), and this page's own backend call must do the same — never append it to a
  URL (which would land in server/proxy access logs) or echo it back in a redirect's query
  string.
- A new generic "admin auth" system for this page. There is no buyer identity yet at this point
  in the flow (the product they're about to deploy doesn't exist until they click Launch) — this
  page's only real security boundary is the Marketplace token's own server-side validation, not a
  login system. Don't build one.
- Hardcoding a specific release version's template URL into the page. The buyer should always get
  a link to the latest stable release unless Loop8 explicitly wants to pin a specific version —
  make the version a small, easily-updated config value, not a code change per release.

## Constraints

- Lives in Loop8's own, already-existing AWS account — this project does not provision a new AWS
  account, only new resources inside the existing one.
- **That account has no Terraform state backend or GitHub OIDC deploy role for non-customer
  infrastructure yet** — this project includes a small, one-time bootstrap (its own state
  bucket + OIDC provider/role, a Loop8-account equivalent of `terraform/bootstrap/`, but entirely
  separate from it — that one is customer-side) before any of the landing-page resources can be
  applied.
- **IaC and CI/CD live in this same repo**: a new Terraform root (not inside the existing
  `terraform/` tree, which is entirely about the customer-deployed product) and a new GitHub
  Actions workflow with its own OIDC trust into Loop8's account. The existing `.github/workflows/
  release.yml` stays exactly as credential-free as it documents itself today — this is a new,
  separate workflow, not a change to that one's trust boundary.
- **Loop8 already controls the `loop8.io` Route 53 hosted zone in this same target account** — the
  ACM certificate (required in `us-east-1` for CloudFront) can use fully automated DNS
  validation in Terraform; no manual CNAME step is needed, unlike the product's existing
  custom-domain flow.
- **The AWS Marketplace listing doesn't exist yet** — this project can be built and tested against
  AWS's documented Marketplace test/sandbox tokens without a live listing; registering this URL
  as the listing's fulfillment endpoint, and creating the listing itself, is a separate, parallel
  task this project doesn't own.
- Domain: `deploy.loop8.io` (a subdomain of the domain Loop8 already owns) — Loop8's call, stated
  here; confirm the exact subdomain name before the plan locks it in, since this is cheap to
  change but should only be decided once.
- The Marketplace token must be resolved server-side via `aws-marketplace:ResolveCustomer`
  (AWS's own API for exactly this purpose) before anything else happens — this is the only
  authentication this flow has, and it is sufficient: AWS itself guarantees a token only resolves
  successfully if it's genuine and unexpired.
- `installer/installer.yaml`'s existing parameters (`AphroditeVersion`, `BundleSha256`,
  `Environment`, `SamlMetadataUrl`, `DeleteData`) are unchanged — `CompanyName` and `AdminEmail`
  are additive new parameters, not a restructuring of the existing template.
- No PII/customer-deployment data is implicated here in the sense the rest of this codebase cares
  about (data residency, no-PII-to-the-LLM) — this is pre-sale lead data (a company name and a
  business email address) stored in Loop8's own account for Loop8's own sales/support use, not
  customer operational data subject to the product's own data-handling rules.

## The parts of the how (decided during this conversation)

- **Architecture:** a static frontend (S3 + CloudFront) calling one small backend API (API
  Gateway + Lambda) — the same pattern the rest of this product already uses, reused here for
  consistency rather than inventing a new stack.
- **Flow:** AWS Marketplace POSTs the buyer's browser to this page's fulfillment endpoint with
  `x-amzn-marketplace-token` in the body → the Lambda calls `ResolveCustomer` → on success, the
  frontend shows the company-name/admin-email form → on submit, the Lambda writes a subscriber
  record (company, admin email, the resolved `CustomerAWSAccountId`/`ProductCode`/
  `CustomerIdentifier`, a timestamp) to a small DynamoDB table, then returns a CloudFormation
  "Launch Stack" quick-create URL with `param_CompanyName`/`param_AdminEmail` pre-filled → the
  buyer clicks it, which opens the AWS CloudFormation console in *their own* account with the
  template and parameters already loaded, exactly like today's installer flow, just pre-filled.
- **Durable tracking:** yes, explicitly decided — a small DynamoDB table in Loop8's account, not
  a one-time pass-through. This is the specific reason Option B (custom page) was chosen over
  AWS Marketplace's free, code-free "Launch CloudFormation" button.
- **Template hosting for the quick-create link:** mirror `installer.yaml` into an S3 bucket in
  Loop8's own account (rather than pointing `templateURL` at a GitHub Release asset) — more
  reliable and avoids depending on GitHub's asset-hosting behavior for something CloudFormation's
  console needs to fetch reliably at launch time. The existing release workflow already produces
  this file; add one step that also copies it to this new bucket.
- **Scope boundary on "admin" and "API security":** the original request also mentioned
  "first-person-to-sign-up becomes admin" and "API can only be used/modified by an admin user" —
  those apply to the *deployed product's* own API and Cognito setup (inside the buyer's account),
  not to this landing page, which has no product and no buyer identity yet. Out of scope here;
  revisit when that piece is brainstormed on its own, as agreed at the start of this conversation.

## Resolved during builder-check (the builder's own calls, not sent back to the human partner)

- **How browser state survives the subscribe-POST → form → submit sequence, without a login
  system and without the Marketplace token ever appearing in a URL.** AWS Marketplace's redirect
  is a full-page POST, which a static S3/CloudFront SPA cannot receive directly — something has
  to turn that POST into a page the SPA can render a form on, and carry the *fact* "this browser
  was just resolved to CustomerAWSAccountId=X" forward to the later submit call. Resolution: the
  fulfillment POST is received by API Gateway/Lambda directly (on its own subdomain,
  `api.deploy.loop8.io`, separate from the static site's `deploy.loop8.io` — simpler to provision
  than CloudFront path-based origin routing, and keeps the static distribution untouched). That
  Lambda calls `ResolveCustomer`, writes a short-lived **pending-resolution** record to DynamoDB
  (the resolved customer/product identifiers, an expiry of ~30 minutes, a single-use flag), and
  302-redirects the browser to the SPA (`https://deploy.loop8.io/setup?session=<opaque-id>`) —
  the opaque ID is a one-time reference Loop8's own backend issued, not the Marketplace token
  itself, the same category of thing as a password-reset link or an OAuth `state` parameter
  (expected to travel in a URL; the anti-goal is about the *Marketplace* token specifically,
  which this never carries). The SPA's submit call sends that session ID in its POST body
  (alongside company name/admin email) to a second endpoint, which looks up the pending record,
  checks it's unexpired and unused, marks it used, writes the final subscriber record, and
  returns the launch link. This means DynamoDB holds two record shapes (pending-resolution,
  short-lived; final subscriber, durable) — a single table with a type discriminator is enough,
  no need for two tables.
- **Whether the Lambda should pin the `ResolveCustomer` response's `ProductCode` against an
  expected value.** Yes — cheap, and protects against a token for a *different* Loop8-seller
  product (if Loop8's seller account ever lists more than one) resolving successfully here but
  being for the wrong product. One constant, checked once.

## What's left to the builder

- Whether `ResolveCustomer` alone is sufficient, or whether `GetEntitlements` should also be
  called at this step (recon note: the existing `MARKETPLACE_READINESS_PLAN.md` already plans a
  `GetEntitlements` check *inside* the deployed app for ongoing access control — this page likely
  only needs `ResolveCustomer` to prove the token is genuine, not a full entitlement check, since
  the buyer hasn't deployed anything yet to be entitled to use).
  - Exact DynamoDB table shape (partition key, any GSI for looking up by company/email later) —
  a reasonable single-table design with a generated subscription ID as the key, not over-built
  for a tracking table with no complex query needs yet.
  - Exact CFN quick-create URL construction (region selection, stack name default, URL-encoding
  of the two parameter values) and where the "current release version" config value lives (an SSM
  parameter in Loop8's account is a reasonable default, consistent with how the product's own
  installer already reads config from SSM).
  - Form validation specifics (email format, company-name length/characters) and what the page
  shows for an invalid/expired/already-resolved token (a plain error state, not a crash — don't
  over-build this into a support flow).
  - Exactly which two new parameters `installer.yaml` needs and how `CompanyName`/`AdminEmail`
  flow from there into the deployed product (e.g., seeding the business config's org name, and
  wherever the admin email ends up being used for the first Cognito invite) — confirm against the
  installer's actual CodeBuild → Terraform `TF_VAR_*` wiring before writing the plan.
