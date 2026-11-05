# Monthly AML Name Export

An **EventBridge-scheduled Lambda** (1st of each month, off by default) that:
1. Pulls the active-client roster + associated persons from **Clio + Estateably**.
2. Deduplicates people across the two systems.
3. Writes a **formatted Excel** workbook to **S3** (and optionally a SharePoint
   review folder) for a human to screen in the in-app Sanction Screener.

**Non-negotiable constraints:**
- 🚫 **No PII to the LLM.** This job is pure structured extraction — it must **never
  call Bedrock**. (SINs/DOBs/beneficiary identifiers never touch the model.)
- 📍 **Data residency:** all compute/storage/logs/outputs stay in the deployment region.
- 🔒 **Read-only** from all source systems. No write-back to Clio/Estateably.
- 🏷️ Every AWS resource carries the `Project = aphrodite` tag (set via the
  Terraform provider `default_tags`).
- 🧑‍⚖️ **Human-in-the-loop:** the file is produced for review + manual upload, never
  auto-submitted.

## Workbook layout

Sheets: `Parties` (the screening set), `Summary`, `Excluded Matters`,
`Excluded Parties`, `Exceptions`, `Missing DOB`, `Instructions`.

- **Role taxonomy.** `Party Role` maps to one of ten canonical roles (Deceased,
  Settlor, Trustee, Executor, Beneficiary, Protector, Attorney, Committee, UBO,
  Counterparty) via `quality.map_role`; unmapped roles (advisors, social
  contacts, generic contacts) move to `Excluded Parties` with a reason, kept for
  audit but not screened.
- **Own-company exclusion.** A configurable own-company name (see
  `quality.is_excluded_party_name`) is excluded as a named party.
- **Trust-entity handling.** A trust/estate/company name is typed
  Trust/Corporation, never Individual, and never carries a person DOB
  (`quality.looks_like_entity`). Trust matters are flagged "party roster
  incomplete" until source rosters are entered.
- **No silent drops.** A matter that errors during build is surfaced on
  `Excluded Matters` rather than dropped.
- **DOB** is written as `YYYY-MM-DD` text (not an Excel date serial). **SIN**
  columns are populated only where the source system exposes a structured
  field (Estateably beneficiaries) — otherwise left for manual entry.
- An **include/exclude designation list** (`designations.py`) can override the
  rule-based taxonomy as a backstop, maintained as an editable workbook.

## Layout

```
backend/lambdas/aml_export/
  handler.py      # EventBridge entry point (cron 1st-of-month)
  export.py       # orchestration: fetch active roster -> collect parties -> dedup
  quality.py      # exclusions / role mapping / normalization / validation
  designations.py # include/exclude designation list loader
  excel.py        # openpyxl writer for the workbook
  storage.py      # S3 write + manifest (audit trail)
  tests/          # unit tests (mocked connectors)
terraform/modules/aml_export/   # Lambda + IAM (secrets/KMS read, S3 write) + EventBridge rule
```

Connectors are built the same way as `backend/api/handlers.py::_build_deps` (shared
`services/` — secrets, HTTP, rate-limiting, logging). Dedup keys on normalized
`(full name + DOB)`, falling back to a SIN when present.

## Configuration

| Env / Terraform var | Purpose |
|---|---|
| `APHRODITE_OUTPUTS_BUCKET` | S3 bucket the export is written to |
| `APHRODITE_AML_EXPORT_PREFIX` | Key prefix (default `aml-exports`) |
| `APHRODITE_REFERENCE_BUCKET` / `APHRODITE_AML_DESIGNATIONS_KEY` | Optional designation-list override |
| `APHRODITE_NOTIFY_TOPIC_ARN` | SNS topic for "export ready" notifications |
| `aml_export_schedule_enabled` | Enable the monthly EventBridge schedule (off by default) |
| `APHRODITE_OWN_COMPANY_NAMES` (JSON list, default `["Aphrodite"]`) | Party name(s) excluded as the deployment's own company |
| `APHRODITE_INTERNAL_TITLE_KEYWORDS` (JSON list) | Matter title/description keywords that mark a matter as internal/administrative |
| `APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN` (regex) | Matter-number pattern that marks a matter as internal/administrative |

The exact workbook column layout and the own-company exclusion name are
deployment-specific — confirm them per customer before enabling the schedule.
