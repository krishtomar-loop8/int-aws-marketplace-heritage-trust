variable "aws_region" {
  type        = string
  default     = "ca-central-1"
  description = "AWS region for all resources (data residency: Canada)."
}

variable "environment" {
  type        = string
  default     = "poc"
  description = "Deployment environment."
}

variable "custom_domain" {
  type        = string
  default     = ""
  description = <<-EOT
    Custom domain for the SPA. When non-empty, Terraform mints an ACM certificate
    (in us-east-1) for it and exposes the DNS validation records via the
    `acm_validation_records` output. Empty (default) falls back to the
    CloudFront default domain.
  EOT
}

variable "enable_custom_domain" {
  type        = bool
  default     = false
  description = <<-EOT
    Two-phase toggle for the custom domain (the customer manages DNS externally):
      Apply 1 (false): create the cert only, then hand the customer the validation CNAME.
      Apply 2 (true):  once the customer has added the validation record and the
                       cert is ISSUED, attach the domain + cert to CloudFront.
  EOT
}

variable "saml_metadata_url" {
  type        = string
  default     = ""
  description = <<-EOT
    Entra ID SAML "App Federation Metadata URL" for Cognito federation. Supplied
    via the GitHub Actions repo variable SAML_METADATA_URL (TF_VAR_saml_metadata_url).
    Empty until the Entra enterprise app exists; when set, the EntraID SAML IdP is
    created and added to the Cognito app client.
  EOT
}

variable "quarterly_matter_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Default matter set for scheduled quarterly report runs. Empty ⇒ the scheduled
    run is a no-op (safe default); a manual invoke can still pass matter_ids for
    the pilot. Set once HT confirms which matters get quarterly statements.
  EOT
}

variable "quarterly_notify_emails" {
  type        = list(string)
  default     = []
  description = "Internal staff emails to notify when quarterly drafts are ready for review."
}

variable "quarterly_schedule_enabled" {
  type        = bool
  default     = false
  description = "Enable the quarterly EventBridge schedule (cron 06:00 on the 15th of Jan/Apr/Jul/Oct, America/Vancouver). Off by default; drafts land in the SharePoint review folder — nothing is auto-sent."
}

variable "quarterly_qualifying_practice_areas" {
  type        = list(string)
  default     = []
  description = "Clio practice-area names whose open matters get a quarterly statement on scheduled (discover) runs. Case-insensitive. Empty = configure per deployment."
}

variable "quarterly_exclusion_field" {
  type        = string
  default     = "Report Exclusion"
  description = "Name of a Clio checkbox custom field. Ticked = omit the matter even if it would qualify (exclusion-only); unticked/absent = practice-area rule applies. Read from Clio by name."
}

variable "quarterly_beneficiary_emails_enabled" {
  type        = bool
  default     = true
  description = "Address the quarterly cover letter to the matter's Estateably beneficiaries (guardian/representative for a minor/incapable beneficiary). Off = address the Clio client contact (legacy)."
}

variable "quarterly_sharepoint_upload_enabled" {
  type        = bool
  default     = false
  description = "Deposit quarterly drafts into the SharePoint review folder on scheduled runs. Requires a Graph write grant on the target site."
}

variable "quarterly_sharepoint_upload_site" {
  type        = string
  default     = ""
  description = "SharePoint site URL that hosts the Quarterly Reports folder. Empty = configure per deployment."
}

variable "quarterly_sharepoint_upload_folder" {
  type        = string
  default     = "Office Administration/Quarterly Reports"
  description = "Folder path under the site's default document library where drafts are written."
}

variable "aml_export_notify_emails" {
  type        = list(string)
  default     = []
  description = "Internal staff emails to notify when the monthly AML export is ready for review."
}

variable "aml_export_schedule_enabled" {
  type        = bool
  default     = false
  description = "Enable the monthly AML export EventBridge schedule. Off by default (invoke manually)."
}

variable "sanctions_refresh_schedule_enabled" {
  type        = bool
  default     = true
  description = "Enable the monthly sanctions-refresh schedule. On by default — it only writes public reference data to S3 (no staff-facing output); keeps the screener's watchlists current."
}

variable "integration_health_schedule_enabled" {
  type        = bool
  default     = true
  description = "Enable the hourly integration health-probe EventBridge schedule (backs the Integrations tab). On by default — it only runs read-only connector checks and writes to an internal table (no staff-facing output), keeping the Integrations tab live. A manual probe invoke was confirmed to write the table before enabling."
}

variable "sharepoint_sites" {
  type = list(string)
  # The crawler is hardened for large legacy document libraries (per-page delta
  # checkpointing + concurrent downloads + runtime budget + self-continue), so
  # it drains them across invocations. Empty by default — configure the
  # Sites.Selected-granted site URLs per deployment.
  default = []
}

variable "sharepoint_crawl_schedule_enabled" {
  type        = bool
  default     = false
  description = "Enable the nightly SharePoint crawl schedule. Off until the site set is confirmed (run manually for the initial backfill)."
}

variable "ab_runner_enabled" {
  type        = bool
  default     = false
  description = "Create the temporary A/B model-measurement Lambda. Off by default; flip on to run the model comparison, then off again."
}
