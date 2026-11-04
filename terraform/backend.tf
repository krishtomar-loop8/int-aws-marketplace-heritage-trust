terraform {
  # Partial backend config: bucket, key and region are supplied at `terraform init`
  # time via `-backend-config` (or a `backend.hcl` file, gitignored) so this file
  # never bakes in a specific AWS account ID or region. See terraform/bootstrap/README.md.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
