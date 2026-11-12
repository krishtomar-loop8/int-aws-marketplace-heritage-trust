# Customer Setup Guides Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every connector card a real, written setup guide (closing the Graph card's
dead "see the setup guide" reference) and the four guides themselves — the parts of
onboarding that can't be automated: creating a Clio Private App, a QuickBooks app,
registering an Entra enterprise app + admin consent, and granting SharePoint
`Sites.Selected`.

**Architecture:** Four static HTML files, shipped in `frontend/public/` (already copied
verbatim into the build by Vite — the same mechanism that already serves `config.json`),
linked from each connector card.

**Tech Stack:** Static HTML, React/TypeScript (the four card components).

**Spec:** `docs/superpowers/specs/2026-11-10-phase3-completion-design.md` (§10)

## Global Constraints

- Plain, accurate prose with exact console navigation steps — no screenshots (spec §3: no
  way to capture real ones without a live account).
- Served as static files under `frontend/public/guides/`, reachable as
  `/guides/<name>.html` — no new infrastructure, no server-side rendering.
- This plan is independent of `docs/superpowers/plans/2026-11-10-deployment-config.md` —
  it only touches the four existing `ConnectorCard.tsx` components, not the new Deployment
  Config tab that plan adds.

## Review Focus

- A guide reachable by a stale/incorrect link is worse than no link — every link added in
  Task 2 must point at a filename Task 1 actually created; verified by the build step, not
  by eye.
- The Graph card's guide must cover admin consent specifically (the step every other guide
  skips) — a customer admin who only grants `User.Read` and never consents to the app's
  actual requested permissions is the most common failure mode for this one connector.

---

### Task 1: Write the four guides

**Files:**
- Create: `frontend/public/guides/clio-private-app.html`
- Create: `frontend/public/guides/quickbooks-app.html`
- Create: `frontend/public/guides/entra-admin-consent.html`
- Create: `frontend/public/guides/sharepoint-sites-selected.html`

**Interfaces:**
- Consumes: nothing.
- Produces: four static files at these exact paths — Task 2 links to them by this exact
  naming.

- [ ] **Step 1: Write `clio-private-app.html`**

Plain HTML (a `<title>`, a few headings, numbered steps — no framework, no build step).
Content: sign in to the firm's own Clio account → Settings → Developer Apps (or the current
Clio menu path) → create a new Private App → select read-only scopes matching this
product's needs (matters, contacts, notes, activities, calendar/tasks, trust/bank
accounts, bills, documents, communications — per CLAUDE.md's Clio source-system notes) →
copy the Client ID/Secret into the Setup screen's Clio card → note the Clio pod (US vs CA)
the firm's account lives on, since the OAuth app must be authorized on that same pod.

- [ ] **Step 2: Write `quickbooks-app.html`**

Content: sign in to the Intuit Developer dashboard → create an app under the firm's own
Intuit account → add the Accounting scope → note the Client ID/Secret → the redirect URI
this deployment's Setup screen displays must be added to the app's allowed redirect URIs
exactly as shown (copy-paste, not retyped) → choose sandbox vs. production environment to
match what's entered in the Setup screen's QuickBooks card.

- [ ] **Step 3: Write `entra-admin-consent.html`**

Content: sign in to the Entra admin center as a Global Administrator → register a new
enterprise app → add the Microsoft Graph **application** permissions this product needs
(`User.Read.All`, `Mail.Read`, `Sites.Selected` — confirm the exact set against
`backend/connectors/graph/connector.py`'s actual Graph calls before finalizing this list)
→ **grant admin consent** for the tenant (the step most often missed — without it, every
call fails with a permissions error even though the app registration looks complete) →
create a client secret → copy Tenant ID/Client ID/Client Secret into the Setup screen's
Graph card.

- [ ] **Step 4: Write `sharepoint-sites-selected.html`**

Content: for each SharePoint site this deployment should index, grant the Graph app's
`Sites.Selected` permission read access to that specific site (via Microsoft Graph's
site-permission API or SharePoint admin center, whichever is current) → the site's full
URL (as granted) is what goes into the Deployment Config tab's SharePoint sites field, not
a display name.

- [ ] **Step 5: Verify**

Run `npm run build` in `frontend/` and confirm all four files exist under `dist/guides/`.

- [ ] **Step 6: Commit**

```bash
git add frontend/public/guides/
git commit -m "docs: add the four customer-facing connector setup guides"
```

---

### Task 2: Link each connector card to its guide

**Files:**
- Modify: `frontend/src/components/ConnectorCard.tsx`

**Interfaces:**
- Consumes: Task 1's four file paths.
- Produces: nothing later tasks consume.

- [ ] **Step 1: Add a hint + link to `QuickBooksCard`, `ClioCard`, `EstateablyCard`**

Add a `<p className="connector-card-hint">` above each card's form, in the same style as
`GraphCard`'s existing hint, each linking to its own guide: `ClioCard` →
`/guides/clio-private-app.html`; `QuickBooksCard` → `/guides/quickbooks-app.html`.
`EstateablyCard` has no corresponding guide (service-account credentials, not an OAuth app
a customer registers) — skip it; its existing lack of a hint is correct, not a gap.

- [ ] **Step 2: Update `GraphCard`'s existing dead reference**

Replace the unlinked "see the setup guide" text with a link to
`/guides/entra-admin-consent.html`.

- [ ] **Step 3: Typecheck and build**

Run: `npx tsc -b` (in `frontend/`) and `npm run build`.
Expected: both clean; confirm (by reading the built `dist/index.html`-adjacent assets, or
by grep on the built JS bundle) that each href matches a file Task 1 created.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ConnectorCard.tsx
git commit -m "feat: link each connector card to its setup guide"
```
