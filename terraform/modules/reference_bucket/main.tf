data "aws_caller_identity" "current" {}

# Mutable reference-data bucket for the Sanction Screener's watchlist bundle.
# The monthly sanctions-refresh Lambda overwrites sanctions/bundle.json here, so
# — unlike the immutable outputs bucket — this bucket has NO Object Lock (the
# object is meant to be replaced). Versioning is on so prior bundles remain
# recoverable. KMS-encrypted at rest, private, all in ca-central-1 (data
# residency). Only public government sanctions data lives here — never PII.
resource "aws_s3_bucket" "reference" {
  bucket = "aphrodite-reference-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "reference" {
  bucket = aws_s3_bucket.reference.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reference" {
  bucket = aws_s3_bucket.reference.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "reference" {
  bucket                  = aws_s3_bucket.reference.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
