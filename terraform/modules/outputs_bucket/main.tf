data "aws_caller_identity" "current" {}

# Immutable outputs bucket for generated artifacts (D5 quarterly report drafts;
# D6 AML export will reuse it). Versioning + Object Lock give the immutable-
# retention posture D7 requires: re-generating a report overwrites its key but
# every prior version is retained and locked for the retention window. KMS-
# encrypted at rest, all in ca-central-1 (data residency), no public access.
resource "aws_s3_bucket" "outputs" {
  bucket              = "aphrodite-outputs-${data.aws_caller_identity.current.account_id}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "outputs" {
  bucket = aws_s3_bucket.outputs.id
  versioning_configuration {
    status = "Enabled"
  }
}

# GOVERNANCE (not COMPLIANCE) so an admin with s3:BypassGovernanceRetention can
# still clean up the POC; every write is retained/immutable for the window.
resource "aws_s3_bucket_object_lock_configuration" "outputs" {
  bucket = aws_s3_bucket.outputs.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.outputs]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "outputs" {
  bucket = aws_s3_bucket.outputs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "outputs" {
  bucket                  = aws_s3_bucket.outputs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
