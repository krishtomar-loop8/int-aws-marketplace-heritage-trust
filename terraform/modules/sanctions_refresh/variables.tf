variable "aws_region" {
  type        = string
  description = "AWS region (data residency: Canada)."
}

variable "source_dir" {
  type        = string
  description = "Directory containing the built Lambda package (code + deps)."
}

variable "reference_bucket" {
  type        = string
  description = "Reference bucket to write the refreshed sanctions bundle to."
}

variable "reference_bucket_arn" {
  type        = string
  description = "ARN of the reference bucket (scopes the s3:PutObject grant)."
}

variable "kms_key_arn" {
  type        = string
  description = "CMK ARN used to encrypt the bundle at rest (SSE-KMS)."
}

variable "schedule_enabled" {
  type        = bool
  default     = true
  description = "Enable the monthly refresh schedule. On by default — public reference data only, no staff-facing output."
}

variable "schedule_expression" {
  type        = string
  default     = "cron(0 3 1 * ? *)"
  description = "Schedule: 03:00 on the 1st of each month (offset from the AML export run)."
}

variable "schedule_timezone" {
  type        = string
  default     = "America/Vancouver"
  description = "Timezone for the schedule (Aphrodite is in BC)."
}
