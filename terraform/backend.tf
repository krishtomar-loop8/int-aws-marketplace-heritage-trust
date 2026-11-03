terraform {
  # Partial backend config: bucket/key are supplied at `terraform init` time via
  # `-backend-config` (or a `backend.hcl` file, gitignored) so this file never
  # bakes in a specific AWS account ID. See terraform/bootstrap/README.md.
  backend "s3" {
    region       = "ca-central-1"
    encrypt      = true
    use_lockfile = true
  }
}
