# Security hardening (Marketplace readiness Phase 5)

Source: `MARKETPLACE_READINESS_PLAN.md`'s Phase 5 line item: "IAM audit, dependency/secret
scanning, data-flow doc, security questionnaire." Recon (a read-only repo survey) was
completed before this document; this document turns that recon into a sized, sequenced plan.
Scope confirmed with the user: proceed on the model/judgment basis used for every other phase
in this project rather than a further live Q&A round, since nothing here is a product-behavior
decision — it's auditing and documenting what the product (built in Phases 1-4) already does.

## Intent and the why

Before this product can be submitted to AWS Marketplace (readiness Phase 7) or survive a
prospective customer's own security review, four gaps need closing: nobody has formally audited
the IAM policies this product's own Terraform creates; there is no automated dependency or
secret scanning in CI at all; there is no standalone data-flow document a security reviewer can
read without reverse-engineering it from code; and there is no pre-filled security questionnaire
to hand a prospect, so every inbound ask currently starts from zero.

None of this changes the product's behavior for an existing customer. This is audit, CI
tooling, and documentation — the kind of work a buyer's security team or AWS's own review
expects to see evidence of, not new product functionality.

## Goals

- **IAM audit**: a written audit of every `aws_iam_policy_document`/`aws_iam_role_policy` this
  repo's Terraform defines (both `terraform/` — the customer-deployed product — and
  `loop8-landing/terraform/` — Loop8's own account), recording what each grants and why, and
  flagging anything that's broader than it needs to be. Tighten anything flagged where doing so
  is safe and mechanical; document (not silently accept) anything broader that's genuinely
  constrained by something outside this repo's control.
- **Dependency scanning**: CI gains an automated, report-only check for known-vulnerable
  dependencies, for both the Python backend (`pyproject.toml`) and the frontend
  (`frontend/package.json`), running on every PR the same way the existing `python`/`terraform`
  checks do today (informational — see "Decisions" below for why this doesn't become a blocking
  gate).
- **Secret scanning**: CI gains an automated, report-only check that a new commit never
  introduces a credential, API key, or private key — scoped to new commits going forward, not a
  full-history scan (see "Decisions" below).
- **Automated dependency updates**: a Dependabot config so dependency bumps (and the new
  scanners' own false-positive/version churn) are routine PRs, not something a human has to
  remember to do.
- **Data-flow document**: a detailed version, readable by someone who has never seen this
  codebase, tracing every path data takes through the product — every connector, every data
  store, every trust/region boundary it crosses, and what's encrypted where — building on (not
  duplicating) `CLAUDE.md`'s existing architecture section and Critical Constraints; plus a short
  public summary version. See "Decisions" below for why this is split in two and where each half
  lives.
- **Security questionnaire**: a filled-in, genericized "vendor security overview" document
  covering the categories a buyer's security team or procurement process typically asks about
  (data residency, encryption, access control, incident response, business continuity,
  subprocessors/third parties, compliance posture) — something that can be handed over today and
  revised later for a specific buyer's own questionnaire format, not written to one. Also split
  into a detailed version and a public summary — see "Decisions" below.

## Non-goals

- **No new security *feature*** (no new auth mechanism, no new encryption scheme, no WAF, no
  GuardDuty/Security Hub setup). This phase audits and documents the existing design; it doesn't
  add new product capability. (A later, separate initiative could stand up AWS-native security
  services — out of scope here.)
- **No compliance certification** (SOC 2, ISO 27001, etc.). The questionnaire describes the
  product's actual security posture; it doesn't claim a certification the product doesn't have.
- **No penetration test or red-team exercise.** That's a distinct, later activity (plausibly
  informed by this phase's data-flow doc, but not part of it).
- **No change to the account-level IAM constraint** that `terraform/bootstrap/bootstrap.yaml`'s
  own existing comment documents (the deploying account denies attaching AWS's `PowerUserAccess`
  managed policy, forcing a curated inline policy as the practical workaround). This phase
  documents that constraint and its blast radius; it does not attempt to engineer around it.

## Anti-goals (would make this a failure even if it "works")

- A security questionnaire so generic it doesn't actually answer anything (e.g., "we take
  security seriously" instead of "data at rest is encrypted via SSE-KMS; see `terraform/modules/
  kms`"). Every section must cite the real mechanism, with a repo path where one exists.
- A dependency/secret scanner wired into CI that's so noisy (true positives buried in false
  positives) that it gets ignored or disabled within a month. Pin to a reasonably mature,
  low-noise tool rather than the most exhaustive one available.
- IAM "tightening" that breaks a real deployment. Any policy change must be verified via
  `cfn-lint`/`terraform validate` (already this project's standing local-only verification
  practice) and reasoned through against every resource that policy's role actually touches —
  not just typed from the audit findings without re-checking against the real Terraform.

## Decisions (resolved during builder-check)

A fresh-eyes builder-check on this document surfaced two real gaps the recon alone couldn't
answer, both now resolved:

- **This repo is public** (confirmed via `gh repo view`). The data-flow doc and security
  questionnaire are both designed to cite real, specific mechanisms (IAM scoping, architecture
  internals) per the Anti-goals — exactly the detail an anonymous public reader shouldn't get for
  free about a trust/estate-law product's attack surface, and a filled-in questionnaire is
  normally handed to a specific prospect, not committed to a public tree. **Resolution: split
  each document in two.** A short, generic public summary (what categories are covered, that a
  data-flow mapping and a security questionnaire exist, high-level claims with no specific
  resource names/ARNs/IAM statement text) is committed to this repo at
  `docs/security/data-flow-summary.md` and `docs/security/security-overview-summary.md`. The
  detailed versions (the real content — actual IAM policy excerpts, actual resource names, the
  fully-answered questionnaire) are **not committed to this repo** — they go to a location
  outside version control the user designates (e.g. a private Google Doc/Notion page); this
  phase's own tasks produce the detailed content as a deliverable file in this plan's own
  worktree, which the user then moves to that private location themselves after review, rather
  than this plan committing it anywhere public.
- **`main` has no branch protection today** (confirmed via `gh api
  repos/.../branches/main/protection` → 404) — a red CI check doesn't block any merge currently.
  **Resolution: the new dependency/secret scanners are report-only CI jobs**, matching every
  existing check's actual enforcement level today (informational, not gating) — this phase does
  **not** configure branch protection or make any check required. If that changes later, it's a
  separate, explicit decision, not a side effect of adding these jobs.
- **Secret-scan scope: new commits only, not full git history.** Scanning the four already-merged
  phases' worth of history risks surfacing an old false-positive-shaped string (e.g. a mock OAuth
  token in a connector test fixture) that would need triage or a history rewrite this phase isn't
  sized for. `gitleaks`'s own incremental/PR-diff mode (or equivalent) is the pick — config, not
  a cleanup project.

## Constraints

- **No local deployment, no live AWS mutation from an agent session** — this project's standing
  rule. IAM changes are `cfn-lint`/`terraform validate`-verified locally only; nothing is applied
  for real as part of this phase.
- **No Claude/Co-Authored-By attribution** in any commit message (CLAUDE.md).
- **Every phase ships as its own branch and PR**, reviewed via subagent-driven-development,
  matching every other phase in this project.
- The two repos' worth of Terraform (`terraform/` for the customer product, `loop8-landing/
  terraform/` for Loop8's own account — a separate, already-shipped project) are both in scope
  for the IAM audit, since both are real IAM surfaces this product/org controls. The landing
  page's own Terraform was already independently reviewed for IAM correctness during its own
  build (ACM/CORS/S3-policy-race findings were fixed there) — this audit's job is to look at it
  through a *security-hardening* lens specifically (over-broad grants), not re-litigate its
  already-settled functional correctness.

## Recon findings (resolved before handoff — read this before auditing further)

A read-only repo survey found:

1. **IAM.** Files defining policy documents: `terraform/bootstrap/bootstrap.yaml` and
   `terraform/modules/{aml_export,iam,logging,quarterly_report,sanctions_refresh,search_api,
   sharepoint_kb,web}/main.tf`. Every per-Lambda module (`aml_export`, `quarterly_report`,
   `sanctions_refresh`, `search_api`, `sharepoint_kb`, the shared `iam` module) scopes its
   execution role tightly with named `sid`s — **no wildcard actions found in any of them.**
   `terraform/modules/logging/main.tf`'s one `s3:*` statement is a **Deny** (enforces
   `aws:SecureTransport`), not a grant — a hardening control, correctly read as such.
   `terraform/modules/kms/main.tf`'s `kms:*`/`Resource: "*"` is the account-root principal
   section of a KMS key policy — the standard, expected shape for that resource type, not a
   finding. The one real flagged item: **`terraform/bootstrap/bootstrap.yaml`'s Terraform deploy
   role has wildcard-action grants** (`s3:*`, `bedrock:*`, `kms:*`, `secretsmanager:*`,
   `lambda:*`, `dynamodb:*`, etc., each `Resource: "*"`), with an inline comment already
   explaining why (the account denies attaching `PowerUserAccess`, so this curated wildcard list
   is the practical substitute) — a second statement in the same file correctly scopes
   `iam:CreateRole`/etc. to `arn:...:role/aphrodite/*` and `policy/aphrodite/*` specifically. This
   is exactly the kind of item the audit should document with its justification, and explicitly
   evaluate for resource-level tightening (e.g., scoping `dynamodb:*`/`s3:*` to this product's own
   resource naming prefix/ARN pattern rather than every resource in the account) without
   assuming the service-wildcard shape itself must change.
2. **Dependencies.** `pyproject.toml` (root, Python) and `frontend/package.json` +
   `package-lock.json` are the only manifests; no `requirements.txt`. **No CI workflow runs any
   dependency scanner today** (checked for `pip-audit`, `safety`, `npm audit`, `snyk`, `trivy`,
   `bandit` — none found).
3. **Secrets.** **No secret scanner wired into CI** (checked for `gitleaks`, `trufflehog`,
   `detect-secrets` — none found).
4. **Automated updates.** No `.github/dependabot.yml`, no Renovate config.
5. **Docs.** No data-flow diagram or security-questionnaire document exists yet. `CLAUDE.md`'s
   "Critical Constraints" section already states data-residency, read-only/no-write-back,
   human-in-the-loop, and audit (CloudTrail+CloudWatch) commitments at a prose level — the
   data-flow doc builds on this, it doesn't start from nothing.
6. **GitHub security-tab files.** No `CODEOWNERS`, no `SECURITY.md` anywhere in the repo.

## What's left to the builder

- The exact dependency-scanner tool per ecosystem: `pip-audit` for Python (reads `pyproject.toml`
  directly, low false-positive rate, actively maintained) and `npm audit --audit-level=high` for
  the frontend (zero new dependency — it ships with npm already) are the plan's own
  recommendation, but confirm current best-practice/maintenance status for each before locking
  them into the plan (tool landscape moves; verify via each tool's own docs/release history
  rather than assuming this document's knowledge is current).
- The secret scanner: `gitleaks` (via its own GitHub Action) is this document's pick — mature,
  low-noise, no account/API key of its own required — confirm this is still accurate the same
  way.
- `SECURITY.md`'s exact contents (a standard "how to report a vulnerability" file) — this
  document doesn't specify the reporting email/process; that's a real decision the plan should
  surface back if it can't be answered from existing project conventions (e.g., is there already
  a security-contact email, or does one need to be decided).
- The exact set of IAM statements worth tightening in `bootstrap.yaml` — the recon above flags
  the candidate (resource-scoping the wildcard actions to this product's naming prefix), but the
  plan's own task should re-verify this is safe by checking every resource type's actual ARN
  pattern this product creates before writing the tightened policy, not propose a scope from
  memory.
- The security questionnaire's exact section list and depth — this document names the common
  categories (data residency, encryption, access control, incident response, business
  continuity, subprocessors, compliance posture); the plan should flesh out the specific
  questions within each category a typical vendor-security review asks, grounded in what this
  product's own docs (CLAUDE.md, the data-flow doc this same phase produces) can already answer.
