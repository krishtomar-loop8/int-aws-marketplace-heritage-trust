# terraform

Infrastructure as Code (default region ca-central-1).

- `bootstrap/` — one-time CloudFormation bootstrap (state bucket, OIDC provider, deploy role).
  See `bootstrap/README.md`.
- The Terraform root (backend, providers, modules) is applied via the `terraform.yml`
  GitHub Actions workflow (plan on PR → auto-apply on merge to `main`).

State: S3 backend with **native locking** (`use_lockfile=true`); no DynamoDB. Bucket/key are
supplied at `terraform init` time via `-backend-config` (see `bootstrap/README.md`) rather than
hardcoded in `backend.tf`, since this stack deploys into a different AWS account per customer.
All resources carry a `Project = aphrodite` tag via the provider `default_tags`.

## Modular architecture: per-connector/module toggles

Every deployment can turn individual connectors and modules on or off
(`terraform/variables.tf`). A disabled connector's Secrets Manager secret is
paused (its IAM grants are dropped) — never destroyed, so re-enabling it
retains the stored credential; re-authorizing isn't *required* by this
toggle itself. A token that lapses from disuse while paused (e.g. a
QuickBooks refresh token, which Intuit expires after ~100 days without a
refresh) still needs a reconnect regardless — that is the token's own
expiry, not something re-enabling fixes. A disabled module's Lambda,
schedule, and IAM are not deployed at all (`count = 0`).

| Variable | Default | Dependency |
|---|---|---|
| `clio_enabled` | `true` | — |
| `estateably_enabled` | `true` | — |
| `quickbooks_enabled` | `true` | — |
| `graph_enabled` | `true` | — |
| `quarterly_enabled` | `false` | requires `clio_enabled` |
| `aml_export_enabled` | `false` | requires `clio_enabled` AND `estateably_enabled` |
| `sanction_screener_enabled` | `false` | — |

`terraform plan`/`apply` fails fast (a `validation` block) if a module is
enabled while a connector it requires is disabled.

**Upgrading an existing deployment that predates these flags:** `quarterly_enabled`,
`aml_export_enabled`, and `sanction_screener_enabled` default to `false`, matching the
new-buyer default — but the `quarterly_report`/`aml_export`/`sanctions_refresh` modules
were unconditional before this toggle existed. Before applying this change against a
deployment that already has one of those modules running, set its flag to `true`
explicitly (e.g. `-var='quarterly_enabled=true'`) so the apply doesn't disable (and
destroy) a module already in use. A fresh deployment with no prior state is unaffected.
