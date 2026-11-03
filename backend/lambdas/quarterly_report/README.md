# Quarterly Report Draft Generator

An EventBridge-scheduled (and manually-invokable) Lambda that produces, **per trust
matter**, a client-facing **quarterly trust statement draft**, and writes it to the
immutable outputs bucket for human review. Nothing is auto-sent — a person reviews,
corrects (notably the investment total), and sends it.

**Hard constraints:** read-only from Clio (the only source); all outputs at rest in
the deployment region; human-in-the-loop (drafts only, never sent); every AWS
resource carries the `Project = aphrodite` tag via the provider `default_tags`.

## What it generates

| Artifact | Generated? | Source |
|---|---|---|
| **Cover letter** (.docx) | ✅ yes | Clio matter + client contact + computed balances |
| **Trust ledger** (.pdf) | ✅ yes | Clio trust `bank_account` + `bank_transactions` |
| **Investment portfolio statement** (.pdf) | ❌ no | Third-party custodian — attached manually at review |

The cover letter presents an itemized **"Assets Held"** breakdown (Cash / Real
Property / Investments / GICs / Total Assets). Each Clio trust account is
classified by a configurable name-pattern rule (`classification.py`). The
outside-counsel investment portfolio total lives only on a third-party custodian
statement, so that line is a **highlighted staff fill-in** and Total Assets stays
pending until entered; a reviewer checklist and attention flags (unclassified
account, stale real-property valuation) are appended to the draft.

## Asset classification

Each Trust-typed Clio account is classified into **Cash / Real Property /
Investments / GICs** by a case-insensitive substring match on the account name:

| Category | Contains (default) |
|---|---|
| Real Property | `Real Property` |
| GICs | `GIC` |
| Investments | `External Bank`, `Investment Account` |
| Cash | everything else |

The defaults ship in code (`DEFAULT_CLASSIFIER`); additional patterns can be
supplied via an editable workbook in the reference bucket (no redeploy):

- **Location:** `s3://<reference-bucket>/config/account_classification.xlsx`
  (env `APHRODITE_ACCOUNT_CLASSIFICATION_KEY`). Absent/unreadable ⇒ in-code
  defaults.
- **Format:** one sheet, two columns — `Category` and `Pattern` (a substring to
  match). Rows extend the defaults.

An account matching no known pattern is counted under Cash but flagged for
staff review rather than silently bucketed.

Real-property valuation date: the "as at" date shown is the account's most
recent transaction/adjustment date; a recorded value older than 15 months is
flagged for staff review.

## Layout

```
backend/lambdas/quarterly_report/
  handler.py         # EventBridge / manual entry; period + matter selection; batch loop
  generator.py        # Clio -> ReportModel (defensive extraction, running balance, classify)
  classification.py   # account-name -> asset category (defaults + reference-bucket sheet)
  model.py             # ReportModel dataclasses
  period.py            # calendar-quarter bounds/labels + previous-quarter default
  render_docx.py       # cover letter (.docx) via python-docx
  render_pdf.py        # trust ledger (.pdf) via fpdf2
  _fmt.py              # money/date formatting
  storage.py            # S3 writer (period-partitioned keys + manifest, SSE-KMS)
terraform/modules/quarterly_report/   # Lambda + IAM + SNS + EventBridge Scheduler
terraform/modules/outputs_bucket/     # immutable bucket (versioning + Object Lock)
```

**Output layout** (immutable, versioned):
```
s3://<outputs-bucket>/quarterly/<matter>/<YYYY-Qn>/cover-letter.docx
                                           .../trust-ledger.pdf
                                           .../manifest.json
```

**Manual invoke** (schedule is disabled by default):
```bash
aws lambda invoke --function-name <quarterly-report-function> \
  --payload '{"matter_ids":["<clio_matter_id>"],"period":{"year":2026,"quarter":1},"letter_date":"2026-05-11"}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```
Empty payload ⇒ previous complete quarter for the configured matter set.

**Matter selection (in precedence order):**
1. Explicit `matter_ids` in the event (the on-demand portal path) — always wins.
2. `{"discover": true}` (the scheduled EventBridge target) — the set is discovered
   from Clio by **practice area**, minus any matter with the exclusion checkbox
   ticked (see `selection.py`).
3. Otherwise the `APHRODITE_QUARTERLY_MATTER_IDS` env list (empty ⇒ no-op).

**Dry run** (list the discovered set without generating) — use for validation
before enabling the schedule:
```bash
aws lambda invoke --function-name <quarterly-report-function> \
  --payload '{"discover_only":true}' --cli-binary-format raw-in-base64-out /dev/stdout
```

## Configuration (env / Terraform vars)

- `APHRODITE_QUARTERLY_MATTER_IDS` (`quarterly_matter_ids`) — explicit fallback matter set.
- `APHRODITE_QUARTERLY_QUALIFYING_PRACTICE_AREAS` (`quarterly_qualifying_practice_areas`) —
  Clio practice-area names that qualify on discover runs. Empty by default.
- `APHRODITE_QUARTERLY_EXCLUSION_FIELD` (`quarterly_exclusion_field`) — name of a Clio
  checkbox custom field; ticked omits the matter regardless of practice area.
- `quarterly_schedule_enabled` — turn on the EventBridge schedule
  (`cron(0 6 15 1,4,7,10 ?)`, America/Vancouver) once the discovered set is validated.
- `quarterly_notify_emails` — staff addresses subscribed to the "drafts ready" SNS topic.
- `quarterly_beneficiary_emails_enabled` — address the letter to Estateably beneficiaries
  instead of the Clio client contact.
- `quarterly_sharepoint_upload_enabled` / `quarterly_sharepoint_upload_site` /
  `quarterly_sharepoint_upload_folder` — optional SharePoint delivery on scheduled runs
  (requires a Graph write grant on the target site).

**Versioning:** each quarter is a distinct key; re-generating a quarter overwrites
its key but S3 versioning + Object Lock retain every prior generation immutably.
