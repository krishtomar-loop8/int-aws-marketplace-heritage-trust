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
    the pilot. Set once the matter set for quarterly statements is confirmed.
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

# -- Phase 1: per-deployment business-config knobs (Marketplace readiness) --

variable "org_name" {
  type        = string
  default     = "Aphrodite"
  description = "Deployment's organization display name, used in the matter-search system prompt. Does NOT drive the AML own-company exclusion check — see own_company_names."
}

variable "org_office_address" {
  type        = string
  default     = "545 Clyde Avenue, West Vancouver"
  description = "Deployment's office address, used by the matter-search system prompt's care-of/mailing-address rule."
}

variable "excluded_sharepoint_folders" {
  type        = list(string)
  default     = ["hr", "human resources", "finance"]
  description = "SharePoint folder names excluded from search results and from the crawler's index. Case-insensitive."

  validation {
    condition     = alltrue([for f in var.excluded_sharepoint_folders : trimspace(f) != ""])
    error_message = "excluded_sharepoint_folders entries must not be blank (a blank entry would match every folder name)."
  }
}

variable "own_company_names" {
  type        = list(string)
  default     = ["Aphrodite"]
  description = "Party name(s) treated as the deployment's own company and excluded from AML screening (matched as a name prefix)."

  validation {
    condition     = alltrue([for n in var.own_company_names : trimspace(n) != ""])
    error_message = "own_company_names entries must not be blank (a blank entry would match every party name as a prefix)."
  }
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

  validation {
    condition     = alltrue([for k in var.internal_title_keywords : trimspace(k) != ""])
    error_message = "internal_title_keywords entries must not be blank (a blank entry would match every matter)."
  }
}

variable "internal_matter_number_pattern" {
  type        = string
  default     = "^\\s*(2100-\\d+|999999)\\b"
  description = "Regex matching matter numbers that are internal/administrative, excluded from AML screening."

  validation {
    condition     = trimspace(var.internal_matter_number_pattern) != ""
    error_message = "internal_matter_number_pattern must not be blank (an empty pattern matches every matter number)."
  }
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

# -- Connector onboarding & reconnect (Setup screen) -------------------------
# app_base_url is not a variable here — it's derived from the SPA's own
# origin(s) (see local.web_origins in main.tf), which already accounts for
# the custom domain, so a deployer never has to supply it separately.

variable "integrations_alert_emails" {
  type        = list(string)
  default     = []
  description = "Staff emails subscribed to the 'connector down' SNS alert topic."
}

# -- Modular architecture (Phase 2) ------------------------------------------

variable "clio_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to Clio."
}
variable "estateably_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to Estateably."
}
variable "quickbooks_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to QuickBooks."
}
variable "graph_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to Microsoft Graph (Outlook/SharePoint)."
}

variable "quarterly_enabled" {
  type        = bool
  default     = false
  description = "Whether the Quarterly Report Draft Generator module is deployed."
  validation {
    condition     = !var.quarterly_enabled || var.clio_enabled
    error_message = "quarterly_enabled requires clio_enabled (Quarterly Reports reads Clio exclusively)."
  }
}
variable "aml_export_enabled" {
  type        = bool
  default     = false
  description = "Whether the Monthly AML Name Export module is deployed."
  validation {
    condition     = !var.aml_export_enabled || (var.clio_enabled && var.estateably_enabled)
    error_message = "aml_export_enabled requires both clio_enabled and estateably_enabled."
  }
}
variable "sanction_screener_enabled" {
  type        = bool
  default     = false
  description = "Whether the Sanction Screener module (and its monthly watchlist refresh) is deployed."
}
