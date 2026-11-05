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
  description = "Secrets Manager ARNs the Lambda may read (Clio for the trust ledger)."
}

variable "clio_secret_arn" {
  type        = string
  description = "Clio secret ARN the Lambda may write back to (rotated OAuth refresh token)."
}

variable "kms_key_arn" {
  type        = string
  description = "CMK ARN used to decrypt the Clio secret and encrypt outputs (SSE-KMS)."
}

variable "outputs_bucket" {
  type        = string
  description = "Name of the immutable outputs bucket to write drafts to."
}

variable "outputs_bucket_arn" {
  type        = string
  description = "ARN of the immutable outputs bucket (scopes the s3:PutObject grant)."
}

variable "reference_bucket" {
  type        = string
  description = "Name of the mutable reference bucket holding the account-classification config sheet."
}

variable "reference_bucket_arn" {
  type        = string
  description = "ARN of the reference bucket (scopes the s3:GetObject grant on config/*)."
}

variable "matter_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Default matter set for scheduled runs (APHRODITE_QUARTERLY_MATTER_IDS). Empty ⇒ the
    scheduled run is a no-op; a manual invoke can still pass matter_ids. Auto-
    discovery of all trust matters is a future enhancement (confirm the Clio
    filter with the customer first).
  EOT
}

variable "notify_emails" {
  type        = list(string)
  default     = []
  description = "Internal staff emails subscribed to the 'drafts ready' SNS topic."
}

variable "schedule_enabled" {
  type        = bool
  default     = false
  description = "Enable the quarterly EventBridge schedule. Off for the POC pilot (invoke manually)."
}

variable "schedule_expression" {
  type        = string
  default     = "cron(0 6 15 1,4,7,10 ? *)"
  description = "Schedule: 06:00 on the 15th of the month after each quarter end."
}

variable "schedule_timezone" {
  type        = string
  default     = "America/Vancouver"
  description = "Timezone for the schedule (Aphrodite is in BC)."
}

variable "sharepoint_upload_enabled" {
  type        = bool
  default     = false
  description = <<-EOT
    Deposit drafts into the SharePoint review folder on scheduled runs. Off until
    the Graph app holds a write grant on the target site (Sites.Selected write) —
    see docs/handoffs/sharepoint-write-grant.md. Reads the shared heritatrust/graph
    secret (already in secret_arns); no new AWS IAM.
  EOT
}

variable "sharepoint_upload_site" {
  type        = string
  default     = ""
  description = "Target SharePoint site URL, e.g. https://<tenant>.sharepoint.com/sites/AphroditeInternalSite."
}

variable "sharepoint_upload_folder" {
  type        = string
  default     = ""
  description = "Folder path under the site's default library, e.g. 'Office Administration/Quarterly Reports'."
}

variable "matter_delay_seconds" {
  type        = number
  default     = 1.2
  description = "Delay between matters in a batch run to pace the source APIs (Clio ~50 req/min)."
}

variable "qualifying_practice_areas" {
  type        = list(string)
  default     = ["Trustee", "Committee", "Power of Attorney"]
  description = <<-EOT
    Clio practice-area names whose open matters get a quarterly statement on
    scheduled (discover) runs. Case-insensitive.
  EOT
}

variable "exclusion_field" {
  type        = string
  default     = "Report Exclusion"
  description = <<-EOT
    Name of the Clio "Report Exclusion" checkbox custom field. Ticked ⇒ the matter
    is omitted, even if its practice area would otherwise qualify (exclusion-only).
    Unticked/absent ⇒ the practice-area rule applies. The customer owns this
    field in Clio; we read it by name.
  EOT
}

variable "beneficiary_emails_enabled" {
  type        = bool
  default     = true
  description = <<-EOT
    Address the cover letter to the matter's Estateably beneficiaries (and the
    guardian/representative for a minor/incapable beneficiary). Off ⇒ address
    the Clio client contact (legacy behavior). Degrades
    to the Clio client per matter when Estateably has no matching estate.
  EOT
}

variable "letterhead_name" {
  type        = string
  default     = "Aphrodite"
  description = "Organization name shown on the quarterly-report letterhead."
}

variable "letterhead_address" {
  type        = string
  default     = "220-545 Clyde Avenue, West Vancouver, BC V7T 1C5 | 778.742.5005 | aphrodite.example"
  description = "Address line shown on the quarterly-report letterhead."
}

variable "letterhead_signoff" {
  type        = string
  default     = "APHRODITE"
  description = "Signoff company name shown on the quarterly-report cover letter."
}

variable "letterhead_privacy_notice" {
  type        = string
  default     = "Our privacy practices are described at www.aphrodite.example/privacy."
  description = "Privacy disclosure line shown at the foot of the quarterly-report cover letter."
}
