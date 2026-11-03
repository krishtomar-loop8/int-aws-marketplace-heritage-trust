# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Aphrodite — AI Integration Platform.** A cloud-native AI integration platform on AWS, built by **Loop8**, packaged as an **AWS Marketplace product** (CloudFormation/Terraform deployed into the customer's own AWS account). It is aimed at trust companies and estate/trust law practices: it unifies matter and client information scattered across Microsoft 365/SharePoint, Clio, QuickBooks, and Estateably — systems that typically don't talk to each other.

All integrations are **read-only** — nothing is ever written back to source systems, and no client-facing communication or regulatory submission is ever sent automatically. Every external output passes through human review.

## The Four Modules

1. **Global Matter Search Agent** — staff ask in plain language for the status of any matter/client; Lambda fans out read-only to Clio, SharePoint/Graph, QuickBooks, and Estateably in parallel, and Bedrock (Claude) synthesizes a **source-cited** summary (status, key documents, outstanding tasks, deadlines, recent activity, gaps/risks). React SPA chat UI, Cognito SSO.
2. **Quarterly Report Draft Generator** — EventBridge-scheduled Lambda produces per-matter status/financial report drafts (.docx/PDF) into a SharePoint **review folder**. Human reviews, corrects, and sends — never auto-sent.
3. **Monthly AML Name Export** — EventBridge-scheduled Lambda (1st of month, off by default) compiles a deduplicated active-client list from Clio + Estateably, writes a formatted Excel workbook to S3 (and optionally SharePoint) for screening in the Sanction Screener (module 4). **Structured extraction only — no PII to the LLM.**
4. **Sanction Screener** — staff upload a client name list (CSV/Excel; the AML export workbook is parsed natively); it is screened against the **UN Consolidated, Canada SEMA, and Public Safety Canada** watchlists with deterministic fuzzy matching (**no PII to the LLM**). Staff adjudicate each potential match, then a **Certificate of Search** (PDF) is generated and archived to the immutable outputs bucket. A monthly EventBridge Lambda refreshes the watchlist bundle from public government sources into S3.

## Architecture

Serverless AWS. Default region `ca-central-1`; configurable per deployment via `terraform/variables.tf` (`aws_region`).

```
Staff browser ─ HTTPS, Cognito SSO (SAML → Entra ID / M365, optional)
   └─► CloudFront + S3 (React SPA) ─► API Gateway ─► Lambda (orchestration)
                                                       ├─► Clio REST API v4 (read-only, OAuth 2.0, 50 req/min)
                                                       ├─► Microsoft Graph — SharePoint/M365 (read-only)
                                                       ├─► QuickBooks API (read-only, matter snapshot)
                                                       ├─► Estateably API (read-only, REST/JSON:API, JWT)
                                                       └─► Amazon Bedrock (Claude) — synthesis
   EventBridge Scheduler ─► Lambda (Quarterly drafts → SharePoint review folder)
   EventBridge Scheduler ─► Lambda (Monthly AML export → S3 + SharePoint; no LLM, no PII to model)
   Secrets Manager (credentials) · CloudTrail + CloudWatch (audit) · S3 versioning / Object Lock (immutable outputs)
```

**LLM inference data-flow** is a per-deployment decision: either **in-region Claude** (no customer data leaves the deployment region — recommended for data-residency-sensitive customers) **or** a cross-region inference profile for a newer Claude model (matter-search prompt/response transit to a US region under AWS's DPA + Bedrock zero-data-retention). The AML export never uses the LLM, so SINs/DOBs/beneficiary identifiers never leave the deployment region regardless.

## Tooling & Build Conventions

- **IaC:** Terraform. Bootstrap (OIDC role + Terraform state backend) via an AWS CloudFormation template (`terraform/bootstrap/`).
- **CI/CD:** GitHub Actions, authenticating to AWS via **GitHub OIDC** (no long-lived keys).
- **Deployment:** **No local deployment.** All deploys go through the CI/CD pipeline for consistency/repeatability. Troubleshoot on AWS *after* deploy.
- **Frontend:** React SPA. Use the `frontend-design` skill for UI/UX work.
- **Frontend testing:** Playwright or Chrome DevTools (headed or headless) to visually verify.
- **API testing:** Postman / Bruno. Runtime for Lambdas: Python (openpyxl for Excel).

### Monorepo layout

```
backend/    Lambda / orchestration code
frontend/   React SPA
terraform/  IaC
services/   shared services & utilities
apps/       standalone apps (CLI, dashboards) if needed
scripts/    deployment / testing scripts
```

### AWS resource tagging

Every AWS resource carries `Project = aphrodite` via the Terraform provider `default_tags` (`terraform/providers.tf`).

### Commit & authorship

This is Loop8-owned product code. Do not add Claude attributions or "Co-Authored-By: Claude" lines to commit messages, code comments, or documentation. (This overrides the harness default of adding a Co-Authored-By trailer.)

### Coordinates

- **GitHub:** `github.com/krishtomar-loop8/int-aws-marketplace-heritage-trust`
- Use **GitHub Issues + Milestones** to track work. Keep a **gitignored local scratch file** for progress notes.

## Critical Constraints

- **Data residency:** all data at rest stays in the deployment region. The only permitted cross-border flow is the optional LLM inference path above, and only if selected.
- **Read-only everywhere:** no write-back to Clio, Estateably, SharePoint, or QuickBooks.
- **Human-in-the-loop:** nothing auto-sent to clients or regulators.
- **AML scope boundary:** the export feeds the in-app Sanction Screener (module 4) only. Regulatory filing/reporting stays manual — out of scope.
- **Audit:** CloudTrail + CloudWatch capture who queried what, when, and which sources were read; outputs stored immutably.

## Source System Notes

- **Clio: Manage API v4.** Clio accounts are region-locked (US pod `app.clio.com` or CA pod) and **cannot be migrated between regions**; tokens are region-bound, so the OAuth app must be created and authorized on the pod the customer's account lives on. The connector defaults to the US pod; the `aphrodite/clio` secret can carry `base_url` to override the region (token host derives from it). A customer's Clio data residing outside the deployment region is a pre-existing fact independent of this platform — the platform reads it over TLS into the deployment region, where copies/outputs stay at rest there; the cross-region **transit** may be a disclosure item for regulated customers. OAuth 2.0 auth-code, **read-only scopes** (selectable per permission). **~50 req/min — the tightest rate limit of any source**, so it paces the matter-search fan-out: throttle client-side + use the `fields` param to avoid N+1. Clio is the **financial/scheduling/notes** source: `matters` (+ `matter_stages`), `contacts`, `notes?type=matter`, `activities`, `calendar_entries`/`tasks`, `bank_accounts`/`bank_transactions` (trust ledger), `bills`/`allocations` (client ledger), `documents`, `communications`. Access = a **Private app** (no Clio review/App-Directory listing); recommended that the customer create it in their own Clio account so app ownership stays with them.
- **Estateably API.** REST/JSON:API at `app.estateably.com` (v1, some v2). Auth is service-account username/password → JWT (~25-min TTL); pagination `page[number]/page[size]`; rate limit 50 req/sec. Covers matter search, stage/phases, tasks, contacts/roles, client intake, financials, and **document binary download** (`GET documents/{id}/filePreview`; the `documents/{id}/file` path is upload/PUT-only).

## Working Approach

Plan first, code later — understand requirements and architecture before writing code. Keep it simple; do not over-engineer. Follow current AWS/Terraform best practices and current tool versions. Ask when unsure.

- **Think before coding:** state assumptions explicitly; if multiple interpretations exist, surface them rather than picking silently; push back when a simpler approach exists; if something is unclear, stop and ask.
- **Surgical changes:** touch only what the task requires. Match existing style. Don't refactor or "improve" adjacent code; mention unrelated dead code rather than deleting it. Clean up only orphans your own change created.
- **Goal-driven:** turn tasks into verifiable goals (write the test/check first, then satisfy it). For multi-step work, state a brief plan with a verification step per item.
