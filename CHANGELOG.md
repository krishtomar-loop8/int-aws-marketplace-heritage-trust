<!--
Keep a Changelog style. The release workflow (.github/workflows/release.yml) finds a
version's release notes by the heading containing "[<version>]", so release headings must be
`## [x.y.z] - YYYY-MM-DD`.
-->
# Changelog

## [Unreleased]

### Added

- CloudFormation installer for buyer accounts: `installer/installer.yaml` template, inline
  Lambda custom resource, CodeBuild-driven `installer/deploy.sh`, and the operations runbook
  (`installer/README.md`).
- Release workflow publishing `installer.yaml`, `aphrodite-<version>.zip` and its `.sha256`
  as GitHub Release assets.
- Point-in-time recovery on the DynamoDB tables.
- Region-agnostic backend: the Terraform state region is supplied at `terraform init`.
- CloudFront distribution no longer blocks Terraform waiting for propagation.

### Notes

- First-deploy timing gate (plan Task 2) pending: needs the sandbox AWS account.
- End-to-end scenarios (installer-e2e.yml) written; runs pending the sandbox AWS account.
