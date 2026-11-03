variable "kms_key_arn" {
  type        = string
  description = "CMK ARN used to encrypt reference objects at rest (the POC customer-managed key)."
}
