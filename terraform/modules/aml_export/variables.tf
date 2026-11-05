variable "aws_region" {
  type        = string
  description = "AWS region (data residency: Canada)."
}

variable "source_dir" {
  type        = string
  description = "Directory containing the built Lambda package (code + deps)."
}

variable "secret_arns" {
  type        = list(string)
  description = "Secrets Manager ARNs the Lambda may read (Estateably + Clio)."
}

variable "clio_secret_arn" {
  type        = string
  description = "Clio secret ARN the Lambda may write back to (rotated OAuth refresh token)."
}

variable "kms_key_arn" {
  type        = string
  description = "CMK ARN used to decrypt the connector secrets and encrypt outputs (SSE-KMS)."
}

variable "outputs_bucket" {
  type        = string
  description = "Name of the immutable outputs bucket to write the export to."
}

variable "outputs_bucket_arn" {
  type        = string
  description = "ARN of the immutable outputs bucket (scopes the s3:PutObject grant)."
}

variable "reference_bucket" {
  type        = string
  description = "Name of the mutable reference bucket holding Aphrodite's designation list."
}

variable "reference_bucket_arn" {
  type        = string
  description = "ARN of the reference bucket (scopes the s3:GetObject grant on config/*)."
}

variable "notify_emails" {
  type        = list(string)
  default     = []
  description = "Internal staff emails subscribed to the 'AML export ready' SNS topic."
}

variable "schedule_enabled" {
  type        = bool
  default     = false
  description = "Enable the monthly EventBridge schedule. Off for the POC pilot (invoke manually)."
}

variable "schedule_expression" {
  type        = string
  default     = "cron(0 0 1 * ? *)"
  description = "Schedule: 00:00 on the 1st of each month."
}

variable "schedule_timezone" {
  type        = string
  default     = "America/Vancouver"
  description = "Timezone for the schedule (Aphrodite is in BC)."
}

variable "own_company_names" {
  type        = list(string)
  default     = ["Aphrodite"]
  description = "Party name(s) treated as the deployment's own company and excluded from AML screening (matched as a name prefix)."
}

variable "internal_title_keywords" {
  type = list(string)
  default = [
    "accounting",
    "bcfsa communication",
    "finance & compliance",
    "finance and compliance",
    "board of directors",
    "wills bank",
    "suspense",
    "natalie's files",
    "natalies files",
    "aphrodite - administration",
    "aphrodite administration",
  ]
  description = "Matter title/description keywords (case-insensitive) that mark a matter as internal/administrative, excluded from AML screening."
}

variable "internal_matter_number_pattern" {
  type        = string
  default     = "^\\s*(2100-\\d+|999999)\\b"
  description = "Regex matching matter numbers that are internal/administrative, excluded from AML screening."
}
