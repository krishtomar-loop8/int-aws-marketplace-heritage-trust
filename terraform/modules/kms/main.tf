data "aws_caller_identity" "current" {}

# Customer-managed key for Secrets Manager (connector credentials).
# Root-account admin statement lets IAM policies (deploy role + Lambda exec roles)
# govern access; rotation enabled for a strong compliance posture.
resource "aws_kms_key" "secrets" {
  description             = "Aphrodite POC - Secrets Manager encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootAccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      # S3 Vectors (the SharePoint KB vector store) encrypts with this CMK; its
      # indexing service principal needs kms:Decrypt for index create + background
      # maintenance. Scoped to this account's s3vectors buckets + the s3vectors
      # encryption context. See AWS "Data protection and encryption in S3 Vectors".
      {
        Sid       = "AllowS3VectorsServicePrincipal"
        Effect    = "Allow"
        Principal = { Service = "indexing.s3vectors.amazonaws.com" }
        Action    = "kms:Decrypt"
        Resource  = "*"
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:s3vectors:*:${data.aws_caller_identity.current.account_id}:bucket/*"
          }
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
          "ForAnyValue:StringEquals" = {
            "kms:EncryptionContextKeys" = ["aws:s3vectors:arn", "aws:s3vectors:resource-id"]
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/aphrodite-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}
