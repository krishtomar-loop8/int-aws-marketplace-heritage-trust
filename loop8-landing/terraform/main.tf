terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial backend config: bucket, key and region are supplied at
  # `terraform init` time via `-backend-config` (mirrors terraform/backend.tf's
  # convention and installer/deploy.sh's `tf_init()` — see that function for
  # the exact `-backend-config="bucket=..."` / `key=...` / `region=...` style),
  # so this file never bakes in Loop8's account ID or region. `bucket` is
  # Task 1's bootstrap `StateBucketName` output.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}

# Loop8's own AWS account — entirely separate trust boundary from the
# customer-deployed product's `terraform/` tree.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "aphrodite-landing"
      ManagedBy = "terraform"
    }
  }
}

# CloudFront's ACM certificate must live in us-east-1 regardless of where the
# rest of the stack runs (see site.tf). The API Gateway custom domain's own
# certificate (api.tf) is a separate certificate, always requested in
# var.aws_region — the two never need to be the same cert since they cover
# different hostnames.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = "aphrodite-landing"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

# Loop8 already controls this hosted zone in this same account (per the
# design doc's Constraints) — looked up, never created here.
data "aws_route53_zone" "loop8" {
  name         = var.hosted_zone_name
  private_zone = false
}
