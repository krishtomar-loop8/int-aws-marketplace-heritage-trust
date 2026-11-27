# Security Hardening (Marketplace Readiness Phase 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four Phase 5 gaps — an unaudited set of IAM policies, zero dependency/secret
scanning in CI, no data-flow document, and no security questionnaire — without changing any
product behavior, and without leaking specific security-relevant detail into this repo's public
history.

**Architecture:** Seven independent tasks, each producing one self-contained deliverable: one
small, verified IAM policy fix plus an audit document; two new report-only CI jobs (dependency
scan, secret scan) plus a Dependabot config; a `SECURITY.md`/`CODEOWNERS` pair; and two
public-summary-plus-private-detail document pairs (data-flow, security questionnaire). No task
depends on another task's files or interfaces — they can be done in any order, though the plan
lists them in the order the design doc introduces them.

**Tech Stack:** CloudFormation (`bootstrap.yaml`, `cfn-lint`), GitHub Actions (YAML), Markdown.

**Spec:** `docs/superpowers/specs/2026-11-28-security-hardening-design.md`

## Global Constraints

- **No Claude/Co-Authored-By attribution** in any commit message.
- **No local deployment, no live AWS mutation.** `cfn-lint`/`terraform validate`/`terraform fmt`
  are fine locally (read-only); nothing is applied for real.
- **CI scanners are report-only, not merge-blocking.** `main` has no branch protection today;
  this phase does not add any (a separate decision if ever made later).
- **Secret scanning is scoped to new commits/the PR diff, not full git history.**
- **The detailed data-flow doc, detailed security questionnaire, and detailed IAM audit never get
  committed to this (public) repo.** Each writes its detailed version to `.scratch/security/`
  (already-gitignored local scratch — confirmed via `.gitignore`), and only a short, generic
  public summary (no specific resource names, ARNs, or policy statement text) goes to
  `docs/security/` and gets committed. This extends the design doc's own public/private split
  (originally stated for the data-flow doc and questionnaire) to the IAM audit too, for the same
  reason — a plan-level call, noted here rather than re-litigated per task.
- Every phase ships as its own branch/PR, reviewed via subagent-driven-development, matching
  every other phase in this project.

## Review Focus

- **A `.gitignore` miss:** if `.scratch/security/` were ever *not* actually covered by the
  existing `.scratch/` ignore rule, the detailed documents would be stageable by accident. →
  pinned in Task 1 (first task to write a `.scratch/security/` file — confirms the ignore rule
  covers it before any other task relies on the same assumption).
- **A scanner configured to actually block CI** (e.g. an added step with no `continue-on-error`
  that fails the job): since `main` has no branch protection, this wouldn't stop a merge today,
  but it would needlessly mark otherwise-fine PRs red going forward. → pinned in Tasks 2 and 3.
- **A dependency-scanner false positive on a dev-only, vendored, or test-only package** (e.g. the
  screener module's own vendored-verbatim code, noted in `pyproject.toml`'s `[tool.ruff]` comment
  as intentionally close-to-upstream) treated as if it were a real production finding. → pinned in
  Task 2's verification step (confirm the scan actually runs and produces a report, not that it
  finds zero issues — a real scan may legitimately report findings on day one).
- **An IAM policy change that silently drops a permission a resource type still needs** (the
  opposite risk from the audit's one real finding): the plan's own Task 1 must re-verify the
  removal against every resource type this Terraform tree actually creates, not just the one
  service name flagged by recon. → pinned in Task 1.
- **A public summary document that accidentally restates specific detail** from its own private
  counterpart (defeating the entire point of the split) — e.g. naming an actual bucket name,
  table name, or IAM role ARN in the "public" file. → pinned in Tasks 1, 6, and 7's own
  verification steps (grep the public file for telltale real-resource-name patterns before
  committing it).

---

### Task 1: IAM audit + one verified-safe policy fix

**Files:**
- Modify: `terraform/bootstrap/bootstrap.yaml`
- Create: `.scratch/security/iam-audit-detail.md` (detailed, NOT committed)
- Create: `docs/security/iam-audit-summary.md` (public summary, committed)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing consumed by a later task.

**The one verified-safe fix (already confirmed during planning, not left to the builder):**
`terraform/bootstrap/bootstrap.yaml`'s `TerraformDeployRole` grants `cognito-identity:*` in its
`ServicesManagedByTerraform` statement's action list — but grepping every `resource "aws_*"` type
across the entire `terraform/` tree finds no `aws_cognito_identity_pool` (or any other
`cognito-identity`-namespaced resource) anywhere; the only Cognito resources this Terraform
creates (`aws_cognito_user_pool`, `aws_cognito_identity_provider`, `aws_cognito_user_pool_client`,
`aws_cognito_user_pool_domain`, `aws_cognito_user_pool_ui_customization`, `aws_cognito_user_group`)
all belong to the *different*, correctly-already-granted `cognito-idp` namespace. `cognito-identity:*`
is dead weight — safe to remove.

- [ ] **Step 1: Remove the unused grant**

In `terraform/bootstrap/bootstrap.yaml`'s `ServicesManagedByTerraform` statement, remove the
`"cognito-identity:*"` line from the `Action` list. Leave every other action untouched.

- [ ] **Step 2: Verify**

Run: `cfn-lint terraform/bootstrap/bootstrap.yaml`
Expected: no new errors (same clean result as before the edit).

Also re-run the grep that justified this removal yourself, against the current tree, before
committing — confirm it still finds zero `cognito-identity`-namespaced resources (someone may
have added one since this plan was written):
`grep -rhoE 'resource "aws_[a-z0-9_]+"' terraform/ | sort -u | grep cognito`
Expected: only `cognito_user_pool`-family resource types (no `cognito_identity_pool`).

- [ ] **Step 3: Write the detailed audit (NOT committed)**

Create `.scratch/security/iam-audit-detail.md` covering every `aws_iam_policy_document`/
`aws_iam_role_policy`/CloudFormation IAM policy in both `terraform/` (the customer-deployed
product) and `loop8-landing/terraform/` (Loop8's own account). For each: which file/resource it's
attached to, what it grants, and a one-line justification. Structure, grounded in the design
doc's own recon:
- `terraform/bootstrap/bootstrap.yaml`'s `TerraformDeployRole`: both its statements, the
  `cognito-identity:*` removal just made, and why the remaining service-wildcard shape is
  appropriate for a Terraform CI/CD deploy role (resources don't exist yet at policy-authoring
  time) — note the compensating controls already in place: OIDC-only assumption, the
  repo-scoped trust-policy condition, the `/aphrodite/` IAM path restriction on the
  IAM-management statement, and the 1-hour `MaxSessionDuration`.
- Every per-Lambda module (`aml_export`, `quarterly_report`, `sanctions_refresh`, `search_api`,
  `sharepoint_kb`, the shared `iam` module): confirm each scopes its execution role tightly with
  named `sid`s and no wildcard actions (already true per recon — the audit's job is to state this
  as a verified finding, not assume it).
- `terraform/modules/logging/main.tf`'s one `s3:*` statement: confirm (re-read the file) it's a
  **Deny** enforcing `aws:SecureTransport`, not a grant — note explicitly why this is not a
  finding.
- `terraform/modules/kms/main.tf`'s `kms:*`/`Resource: "*"`: confirm it's the account-root
  principal section of a KMS key policy on the key resource itself — the standard shape for that
  resource type, not a finding.
- `loop8-landing/bootstrap/bootstrap.yaml`'s deploy role: read it fresh (it was reviewed for
  *functional* IAM correctness during the landing-page project, not through a security-hardening
  lens) — apply the same wildcard/scoping/compensating-controls analysis as above.

- [ ] **Step 4: Confirm `.scratch/security/` is actually gitignored**

Run: `git check-ignore -v .scratch/security/iam-audit-detail.md`
Expected: a match reported against the existing `.scratch/` rule in `.gitignore`. If this does
NOT report a match, stop — do not commit anything from this task until `.scratch/` (or a more
specific rule) actually covers this path; add the missing `.gitignore` rule first and re-verify.

- [ ] **Step 5: Write the public summary (committed)**

Create `docs/security/iam-audit-summary.md`: a short document stating that an IAM audit was
performed covering both Terraform trees, describing the audit's *methodology and general
findings* at a high level (e.g. "per-service execution roles are scoped with no wildcard grants;
the one CI/CD deploy role uses service-level grants justified by [general, non-specific reason];
one unused grant was found and removed during this audit") — with **no specific resource names,
ARNs, policy statement text, or file paths naming real infrastructure**. Before committing, grep
this file for any token that also appears verbatim in `.scratch/security/iam-audit-detail.md`
that looks like a real resource identifier (a role name, an ARN fragment, a bucket name) and
remove/genericize it if found.

- [ ] **Step 6: Commit**

```bash
git add terraform/bootstrap/bootstrap.yaml docs/security/iam-audit-summary.md
git commit -m "fix: remove unused cognito-identity grant from the Terraform deploy role; add an IAM audit summary"
```

(`.scratch/security/iam-audit-detail.md` is intentionally never `git add`-ed — it's gitignored
and stays local for you to move to wherever you want the detailed version kept.)

---

### Task 2: Dependency scanning in CI (report-only)

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing consumed by a later task.

- [ ] **Step 1: Add a `security-scan` job to `ci.yml`**

Add a new top-level job (sibling to the existing `python` job) named `security-scan`:

```yaml
  security-scan:
    name: security-scan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Python dependency audit (pip-audit)
        continue-on-error: true
        run: |
          pip install pip-audit
          pip-audit -r <(pip install -e ".[dev]" --dry-run --report - 2>/dev/null || pip freeze)

      - uses: actions/setup-node@v4
        with:
          node-version: "20"

      - name: Frontend dependency audit (npm audit)
        continue-on-error: true
        working-directory: frontend
        run: npm audit --audit-level=high
```

Verify current best-practice invocation for `pip-audit` against a `pyproject.toml`-based project
(the exact command above — piping a dry-run install report — is this plan's best guess at a
dependency-free way to audit without a `requirements.txt`; confirm via `pip-audit`'s own
documentation whether it has a more direct `--pyproject` or similar flag before locking in the
exact invocation, since this detail depends on the tool's current version/interface).

Each scan step uses `continue-on-error: true` so a finding (or a tool hiccup) marks that step with
a warning indicator in the Actions UI without failing the job or showing a red "X" on the PR —
report-only, per the Global Constraints.

- [ ] **Step 2: Verify the workflow parses and the job runs**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: no error (valid YAML).

Push this task's branch (or open a draft PR, whichever this project's existing workflow permits
for a quick CI check) is not required for this task's own completion — `actionlint
.github/workflows/ci.yml` if available locally, otherwise the YAML-parse check above, is
sufficient local verification. The real CI run happens once this phase's branch is actually
pushed.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "feat: add a report-only dependency-vulnerability scan to CI"
```

---

### Task 3: Secret scanning in CI (report-only, new commits only)

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing from other tasks. If Task 2 already landed first, add this as a new step in
  the same `security-scan` job rather than a second job; if this task runs first, create the
  `security-scan` job yourself with just this step (Task 2 then adds its steps to the job this
  task created) — whichever task actually lands first in git history defines the job, the second
  just adds steps to it.
- Produces: nothing consumed by a later task.

- [ ] **Step 1: Add a gitleaks step scoped to new commits only**

Add (to the `security-scan` job — create it if Task 2 hasn't landed yet, otherwise add this step
to the job Task 2 created):

```yaml
      - name: Secret scan (gitleaks, this PR's commits only)
        continue-on-error: true
        uses: gitleaks/gitleaks-action@v2
        env:
          GITLEAKS_ENABLE_UPLOAD_ARTIFACT: false
```

Confirm `gitleaks-action@v2`'s current default behavior actually scopes to the PR's diff/new
commits rather than full history by reading its own README (tool defaults can change between
versions) before relying on the default — if it does not default to diff-only scanning on a
`pull_request` event, add whatever explicit configuration (e.g. a `GITLEAKS_CONFIG`/fetch-depth
setting) the tool's own docs specify for that scope, rather than assuming.

- [ ] **Step 2: Verify the workflow still parses**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "feat: add a report-only secret scan (new commits only) to CI"
```

---

### Task 4: Dependabot configuration

**Files:**
- Create: `.github/dependabot.yml`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing consumed by a later task.

- [ ] **Step 1: Write the config**

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "npm"
    directory: "/frontend"
    schedule:
      interval: "weekly"
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

- [ ] **Step 2: Verify**

Run: `python -c "import yaml; yaml.safe_load(open('.github/dependabot.yml'))"`
Expected: no error (valid YAML). Confirm the three `directory` values match where this repo's
actual manifests live (root `pyproject.toml`, `frontend/package.json`, and GitHub Actions
workflow files are always discovered from `.github/workflows/` regardless of `directory` for
that ecosystem — verify this against Dependabot's own current docs rather than assuming, since
ecosystem-specific directory semantics occasionally change).

- [ ] **Step 3: Commit**

```bash
git add .github/dependabot.yml
git commit -m "feat: add Dependabot config for pip, npm, and GitHub Actions"
```

---

### Task 5: `SECURITY.md` + `CODEOWNERS`

**Files:**
- Create: `SECURITY.md`
- Create: `.github/CODEOWNERS`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing consumed by a later task.

**Reporting contact (this plan's own call, since no existing security-contact convention was
found anywhere in this repo — confirmed via a repo-wide grep for `security@`/`support@`/
`contact@` patterns, zero hits):** point `SECURITY.md` at GitHub's own private vulnerability
reporting feature (Security Advisories → "Report a vulnerability") rather than inventing an email
address that doesn't exist yet. This requires no new infrastructure (no mailbox to set up or
monitor) and is the standard mechanism for a public GitHub repo. If the user has an actual
security-contact email already in mind, that's a one-line edit to this file later — not a reason
to block this task on an unanswered question.

- [ ] **Step 1: Write `SECURITY.md`**

A short file: supported versions (this product has no versioned release line in the traditional
sense — state plainly that `main` is the actively supported branch), and "Reporting a
Vulnerability" pointing readers to this repo's GitHub Security Advisories tab for private
disclosure, with a note not to open a public GitHub issue for a security report.

- [ ] **Step 2: Write `.github/CODEOWNERS`**

A single line assigning the repo owner (`krishtomar-loop8`, confirmed via `gh repo view
--json owner` — re-confirm this is still accurate at implementation time) as owner of `*` (every
file) — the minimal, correct starting point for a single-maintainer repo; expand later if/when
more maintainers exist.

- [ ] **Step 3: Verify**

Run: `cat SECURITY.md .github/CODEOWNERS` and read both back — confirm no placeholder text
(`TODO`, `[fill in]`, etc.) was left in either file.

- [ ] **Step 4: Commit**

```bash
git add SECURITY.md .github/CODEOWNERS
git commit -m "docs: add SECURITY.md and CODEOWNERS"
```

---

### Task 6: Data-flow document (public summary + private detail)

**Files:**
- Create: `.scratch/security/data-flow-detail.md` (detailed, NOT committed)
- Create: `docs/security/data-flow-summary.md` (public summary, committed)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing consumed by a later task.

- [ ] **Step 1: Write the detailed data-flow document (NOT committed)**

Create `.scratch/security/data-flow-detail.md`, building on (not duplicating wholesale)
`CLAUDE.md`'s existing Architecture section and Critical Constraints. Trace every real data path:
staff browser → CloudFront/S3/API Gateway/Lambda; each of the four source-system connectors
(Clio, Microsoft Graph/SharePoint, QuickBooks, Estateably) — what data crosses, read-only
direction confirmed, encryption in transit (TLS); the Bedrock/LLM synthesis path, including the
per-deployment in-region-vs-cross-region choice `CLAUDE.md` already documents; each scheduled
job's own data path (Quarterly Reports → SharePoint review folder; AML Export → S3 → Sanction
Screener; Sanctions Refresh → public government sources → S3); encryption at rest (SSE-KMS,
confirmed per-module in Task 1's audit) and the immutable-outputs-bucket (S3 Object Lock)
guarantee; CloudTrail/CloudWatch audit coverage. Use a simple ASCII or Mermaid diagram plus prose
listing every real trust/region boundary crossed and what's encrypted at each hop — this is
exactly the specific, citable detail the design doc's Anti-goals require and exactly what stays
out of the public repo.

- [ ] **Step 2: Confirm the file is gitignored**

Run: `git check-ignore -v .scratch/security/data-flow-detail.md`
Expected: a match (same `.scratch/` rule Task 1 already confirmed covers this directory — if
Task 1 already ran and found a gap, that gap is already fixed; otherwise re-run this check
yourself rather than assuming Task 1's finding still holds).

- [ ] **Step 3: Write the public summary (committed)**

Create `docs/security/data-flow-summary.md`: states that a detailed data-flow mapping exists and
describes its *scope* (which connectors, which jobs, which boundary types it covers) without any
specific resource name, bucket name, table name, or ARN. Before committing, grep this file for
any token that also appears verbatim in `.scratch/security/data-flow-detail.md` that looks like a
real identifier, and remove/genericize it if found.

- [ ] **Step 4: Commit**

```bash
git add docs/security/data-flow-summary.md
git commit -m "docs: add a data-flow documentation summary"
```

---

### Task 7: Security questionnaire (public summary + private detail)

**Files:**
- Create: `.scratch/security/questionnaire-detail.md` (detailed, NOT committed)
- Create: `docs/security/security-overview-summary.md` (public summary, committed)

**Interfaces:**
- Consumes: nothing from other tasks (may read Task 1's and Task 6's `.scratch/security/` files
  for grounding if they've already landed, but doesn't require them — write from `CLAUDE.md` and
  this repo's own architecture/constraints directly if they haven't).
- Produces: nothing consumed by a later task — last task in this plan.

- [ ] **Step 1: Write the detailed questionnaire (NOT committed)**

Create `.scratch/security/questionnaire-detail.md` answering, per the design doc's category list,
with real, specific, citable answers (a repo path or mechanism name per answer, not a generic
claim):
- **Data residency**: cite `CLAUDE.md`'s Critical Constraints and `terraform/variables.tf`'s
  `aws_region` default.
- **Encryption**: at rest (SSE-KMS, per-module — cite Task 1's audit if available, otherwise cite
  the relevant `terraform/modules/*/main.tf` KMS usage directly) and in transit (TLS throughout,
  CloudFront/API Gateway).
- **Access control**: Cognito SSO (optional SAML→Entra ID), admin-gated API mutations (cite
  `_require_admin` in `backend/api/handlers.py`), read-only connector scopes.
- **Incident response**: CloudTrail + CloudWatch audit trail (cite the Critical Constraints
  section), and this task's own new `SECURITY.md` (Task 5) as the vulnerability-reporting channel.
- **Business continuity/backup**: note honestly whatever currently exists (S3 versioning,
  Terraform state backup) and whatever doesn't yet (this phase doesn't invent new backup
  infrastructure — if none exists beyond S3 versioning, say so plainly rather than imply more).
- **Subprocessors/third parties**: AWS (infrastructure), and the four source systems as
  customer-authorized integrations (not subprocessors of Loop8's own — the customer's own data
  stays in their systems; Loop8's product reads it, it doesn't re-host it elsewhere), Anthropic
  (via Bedrock, for the in-region case) / the cross-region inference profile's AWS DPA terms for
  the alternate case.
- **Compliance posture**: state plainly that no third-party certification (SOC 2, ISO 27001,
  etc.) exists today, per the design doc's Non-goals — don't claim one.

- [ ] **Step 2: Confirm the file is gitignored**

Run: `git check-ignore -v .scratch/security/questionnaire-detail.md`
Expected: a match.

- [ ] **Step 3: Write the public summary (committed)**

Create `docs/security/security-overview-summary.md`: names the same category list and states that
a detailed, filled-in version exists and is available on request — no specific mechanism names,
resource identifiers, or the detailed answers themselves.

- [ ] **Step 4: Commit**

```bash
git add docs/security/security-overview-summary.md
git commit -m "docs: add a security overview summary"
```

---

## Self-Review Notes

- **Spec coverage:** all four Phase 5 line items are covered — IAM audit (Task 1), dependency +
  secret scanning (Tasks 2-3) plus the automated-updates goal (Task 4), data-flow doc (Task 6),
  security questionnaire (Task 7). `SECURITY.md`/`CODEOWNERS` (Task 5) wasn't itself a named Phase
  5 line item but is directly implied by the design doc's own recon finding ("no GitHub
  security-tab files exist") and its Goals' general intent — included as a cheap, standard,
  closely-related addition rather than a separate phase.
- **Type/name consistency checked:** Tasks 2 and 3 both target the same new `security-scan` job
  in `ci.yml` — their Interfaces blocks explicitly handle whichever task's implementer runs first
  defining the job and the second adding to it, since this plan doesn't mandate task order within
  a day of execution.
- **Proportion check:** this plan is not dramatically longer than its design doc — most tasks are
  short (config/doc-writing steps), consistent with the work itself being mostly documentation and
  CI config rather than application logic.
