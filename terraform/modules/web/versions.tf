terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
      # The default provider serves the S3 origin + CloudFront; the aliased
      # us-east-1 provider mints the CloudFront ACM certificate.
      configuration_aliases = [aws.us_east_1]
    }
  }
}
