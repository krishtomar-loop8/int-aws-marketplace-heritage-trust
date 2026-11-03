variable "kms_key_arn" {
  type        = string
  description = "CMK used to encrypt the secrets."
}

locals {
  sources = ["clio", "graph", "quickbooks", "estateably"]
}

# Empty secret containers. Values are injected out-of-band (console/CLI) and are
# never stored in git or Terraform state.
resource "aws_secretsmanager_secret" "source" {
  for_each                = toset(local.sources)
  name                    = "aphrodite/${each.key}"
  description             = "Read-only API credentials for ${each.key} (value injected out-of-band)."
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
}
