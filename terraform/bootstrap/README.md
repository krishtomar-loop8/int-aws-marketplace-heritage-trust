# Terraform bootstrap (one-time)

`bootstrap.yaml` creates the chicken-and-egg resources Terraform needs before it can run:

- **S3 state bucket** (`aphrodite-tfstate-<account>-<region>`) — versioned, SSE-S3,
  TLS-only, public access blocked. Terraform uses **S3 native locking** (`use_lockfile=true`)
  — no DynamoDB table.
- **GitHub Actions OIDC provider** (`token.actions.githubusercontent.com`).
- **IAM deploy role** (`aphrodite-gha-terraform-deploy`) assumed by GitHub Actions from
  `repo:<org>/<repo>:*` via OIDC, scoped to the `/aphrodite/` IAM path.

## Apply (assisted CLI, one time)

Requires admin credentials in the target AWS account, region `ca-central-1`:

```bash
aws cloudformation deploy \
  --region ca-central-1 \
  --stack-name aphrodite-bootstrap \
  --template-file terraform/bootstrap/bootstrap.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOrg=<org> GitHubRepo=<repo>

# Read the outputs (feed these into the TF backend + GitHub repo variables):
aws cloudformation describe-stacks \
  --region ca-central-1 \
  --stack-name aphrodite-bootstrap \
  --query 'Stacks[0].Outputs' --output table
```

Outputs map to GitHub repo variables: `StateBucketName` → `TF_STATE_BUCKET`,
`DeployRoleArn` → `AWS_DEPLOY_ROLE_ARN`, `Region` → `AWS_REGION`.

`TF_STATE_BUCKET` is also passed to `terraform init -backend-config="bucket=$TF_STATE_BUCKET" -backend-config="key=aphrodite/terraform.tfstate"` since `terraform/backend.tf` ships without a hardcoded bucket/key.
