# Loop8-account bootstrap (one-time)

`bootstrap.yaml` creates the chicken-and-egg resources Terraform needs before
the `loop8-landing/terraform` stack can run, **in Loop8's own AWS account**
(not a customer's account — this is entirely separate from
`terraform/bootstrap/`, which bootstraps the *buyer-side* product deployment).

- **S3 state bucket** (`loop8-landing-tfstate-<account>-<region>`) —
  versioned, SSE-S3, TLS-only, public access blocked. Terraform uses
  **S3 native locking** (`use_lockfile=true`) — no DynamoDB table.
- **GitHub Actions OIDC provider** (`token.actions.githubusercontent.com`).
- **IAM deploy role** (`loop8-landing-gha-terraform-deploy`) assumed by
  GitHub Actions from `repo:<org>/<repo>:*` via OIDC, scoped to the
  `/loop8-landing/` IAM path and to exactly the services the landing stack's
  Terraform creates (S3, CloudFront, API Gateway, Lambda, DynamoDB,
  Route 53, ACM, CloudWatch Logs) — no broad `service:*` or admin policy.

## Before you apply

If Loop8's account already has a `token.actions.githubusercontent.com` OIDC
provider from another project (an AWS account may only have one provider per
issuer URL), remove the `GitHubOIDCProvider` resource from the template and
point `TerraformDeployRole`'s trust policy `Federated` principal at that
existing provider's ARN instead, before deploying.

## Apply (assisted CLI, one time)

Requires admin credentials in Loop8's AWS account:

```bash
aws cloudformation deploy \
  --region <loop8-account-region> \
  --stack-name aphrodite-landing-bootstrap \
  --template-file loop8-landing/bootstrap/bootstrap.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOrg=krishtomar-loop8 GitHubRepo=int-aws-marketplace-heritage-trust

# Read the outputs (feed these into Task 2's Terraform backend config and
# Task 5's GitHub Actions workflow/repo variables):
aws cloudformation describe-stacks \
  --region <loop8-account-region> \
  --stack-name aphrodite-landing-bootstrap \
  --query 'Stacks[0].Outputs' --output table
```

## Outputs and where they're used next

- `StateBucketName` → the `loop8-landing/terraform` backend config
  (`terraform init -backend-config="bucket=$StateBucketName" ...`), the same
  partial-backend-config pattern `installer/deploy.sh`'s `tf_init()` uses for
  the product's own Terraform.
- `DeployRoleArn` → the `AWS_DEPLOY_ROLE_ARN`-equivalent GitHub Actions
  secret/variable the `loop8-landing-deploy.yml` and
  `loop8-landing-mirror-installer.yml` workflows assume via OIDC
  (`aws-actions/configure-aws-credentials`).
- `OIDCProviderArn` / `Region` — informational, not consumed by a later task
  unless the OIDC-provider-reuse case above applies.
