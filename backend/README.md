# backend

Python AWS Lambda handlers and **read-only** source connectors.

- `connectors/` — one package per source system (`clio`, `estateably`, `graph`, `quickbooks`).
  Connectors implement **GETs only** — no write methods exist in the code.
- Shared utilities live in the top-level `services/` package.

## Search configuration

| Env / Terraform var | Default | Purpose |
|---|---|---|
| `APHRODITE_ORG_NAME` (`org_name`) | `"Aphrodite"` | Organization name used in the matter-search system prompt. Does **not** drive the AML own-company exclusion check — see `APHRODITE_OWN_COMPANY_NAMES` in the [AML export README](lambdas/aml_export/README.md) |
| `APHRODITE_ORG_OFFICE_ADDRESS` (`org_office_address`) | `"545 Clyde Avenue, West Vancouver"` | Office address used by the system prompt's care-of/mailing-address rule |
| `APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS` (`excluded_sharepoint_folders`, JSON list) | `["hr", "human resources", "finance"]` | SharePoint folder names excluded from search results and from the crawler's index |
