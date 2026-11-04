# Aphrodite

AI integration platform for trust companies and estate/trust law practices, built by
Loop8, packaged as an AWS Marketplace product. Serverless AWS (default region
`ca-central-1`, configurable) that unifies matter/client information across Microsoft
365/SharePoint, Clio, QuickBooks, and Estateably. All source integrations are
**read-only**.

See `CLAUDE.md` for architecture, conventions, and constraints.

## Repository layout

| Path | Purpose |
|------|---------|
| `backend/` | Python Lambda handlers and read-only source connectors |
| `services/` | Shared Python utilities (secrets, HTTP client, logging) used across the backend |
| `frontend/` | React single-page app |
| `terraform/` | Infrastructure as Code; `terraform/bootstrap/` holds the one-time CloudFormation bootstrap |
| `scripts/` | Operational / build / test scripts |
| `apps/` | Standalone tools (CLI, dashboards) if needed |
| `installer/` | CloudFormation installer for buyer accounts |

## Deployment model

No local deployment — all infrastructure is applied through the GitHub Actions CI/CD
pipeline (OIDC to AWS, no static keys). The only manual step is the one-time bootstrap
(`terraform/bootstrap/`), applied via the AWS CLI into the customer's own AWS account.
See `terraform/bootstrap/README.md`.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```
