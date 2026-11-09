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
never requires re-entering credentials. A disabled module's Lambda, schedule,
and IAM are not deployed at all (`count = 0`).

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
