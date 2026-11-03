variable "kms_key_arn" {
  type        = string
  description = "CMK ARN used to encrypt objects at rest (the POC customer-managed key)."
}

variable "retention_days" {
  type        = number
  default     = 365
  description = "Object Lock default retention (GOVERNANCE mode) for generated outputs."
}
