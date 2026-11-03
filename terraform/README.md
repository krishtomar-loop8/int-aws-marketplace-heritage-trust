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
