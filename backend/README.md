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

## Connector onboarding & reconnect

Lets a customer admin connect/reconnect each source system from the app's
Setup screen instead of a Loop8 engineer running a script by hand — see
`backend/api/connectors_admin.py` and
`docs/superpowers/specs/2026-11-06-connector-onboarding-design.md`.

Five routes write a validated credential to Secrets Manager; all but one
require the Cognito **`admins`** group (see `_require_admin` in
`backend/api/handlers.py`):

| Route | Admin-gated? | Purpose |
|---|---|---|
| `POST /connectors/estateably` | Yes | Validate + store Estateably service-account credentials |
| `POST /connectors/graph` | Yes | Validate + store Microsoft Graph app-only credentials |
| `POST /connectors/clio/authorize-url` | Yes | Build the Clio out-of-band authorize link |
| `POST /connectors/clio/exchange` | Yes | Exchange a pasted Clio code and store the refresh token |
| `POST /connectors/quickbooks/start` | Yes | Store pending QuickBooks app credentials, return the authorize URL |
| `GET /connectors/quickbooks/callback` | **No** — Intuit's browser redirect carries no auth header; protected instead by the one-time `state` value `/connectors/quickbooks/start` mints | Complete the QuickBooks OAuth exchange |

| Env / Terraform var | Purpose |
|---|---|
| `APHRODITE_QUICKBOOKS_REDIRECT_URI` | Hosted callback URL registered on the QuickBooks app |
| `APHRODITE_APP_BASE_URL` (`app_base_url`) | SPA origin the QuickBooks callback redirects the browser back to |
| `APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN` | SNS topic a connector-down alert publishes to (see `integrations_alert_emails`) |
