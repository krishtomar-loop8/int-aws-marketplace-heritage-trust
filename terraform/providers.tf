provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "aphrodite"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# CloudFront requires its ACM certificate in us-east-1, regardless of where the
# rest of the stack lives. This aliased provider exists only to mint that cert;
# no data is stored here (a certificate is not customer data). Same default tags.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "aphrodite"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
