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
  description = "Secrets Manager ARNs the search Lambda may read (the 4 connectors)."
}

variable "quickbooks_secret_arn" {
  type        = string
  description = "QuickBooks secret ARN the Lambda may write back to (rotated OAuth refresh token)."
}

variable "clio_secret_arn" {
  type        = string
  description = "Clio secret ARN the Lambda may write back to (rotated OAuth refresh token)."
}

variable "estateably_secret_arn" {
  type        = string
  description = "Estateably secret ARN the Lambda may write to (admin-submitted via the Setup screen)."
}

variable "graph_secret_arn" {
  type        = string
  description = "Graph secret ARN the Lambda may write to (admin-submitted via the Setup screen)."
}

variable "app_base_url" {
  type        = string
  description = "The deployed SPA's origin (e.g. CloudFront domain), used to redirect the browser back after the QuickBooks OAuth callback."
}

variable "integrations_alert_emails" {
  type        = list(string)
  default     = []
  description = "Staff emails subscribed to the 'connector down' SNS alert topic."
}

variable "kms_key_arn" {
  type        = string
  description = "CMK ARN used to decrypt the connector secrets."
}

variable "cognito_user_pool_id" {
  type        = string
  description = "Cognito user pool id backing the API JWT authorizer."
}

variable "cognito_app_client_id" {
  type        = string
  description = "Cognito app client id (JWT audience)."
}

variable "allowed_origins" {
  type        = list(string)
  description = "Browser origins allowed to call the API (CORS) — the SPA URL(s)."
}

variable "sharepoint_kb_id" {
  type        = string
  default     = ""
  description = "Bedrock Knowledge Base id for SharePoint document retrieval. Empty ⇒ SharePoint skipped."
}

variable "sharepoint_kb_arn" {
  type        = string
  default     = ""
  description = "Bedrock Knowledge Base ARN (scopes the bedrock:Retrieve grant). Empty ⇒ no grant."
}

variable "outputs_bucket" {
  type        = string
  description = "Immutable outputs bucket the API lists/serves quarterly report drafts from."
}

variable "outputs_bucket_arn" {
  type        = string
  description = "ARN of the outputs bucket (scopes the read grant for the /reports routes)."
}

variable "quarterly_function_name" {
  type        = string
  description = "Quarterly-report Lambda name the API invokes for on-demand generation."
}

variable "quarterly_function_arn" {
  type        = string
  description = "Quarterly-report Lambda ARN (scopes the lambda:InvokeFunction grant)."
}

variable "aml_export_function_name" {
  type        = string
  description = "AML export Lambda name the API invokes for on-demand 'run now'."
}

variable "aml_export_function_arn" {
  type        = string
  description = "AML export Lambda ARN (scopes the lambda:InvokeFunction grant)."
}

variable "aml_schedule_name" {
  type        = string
  description = "AML export EventBridge schedule name the API reads/updates."
}

variable "aml_schedule_arn" {
  type        = string
  description = "AML export schedule ARN (scopes the scheduler:Get/UpdateSchedule grant)."
}

variable "aml_scheduler_role_arn" {
  type        = string
  description = "Scheduler role ARN passed on UpdateSchedule (scopes the iam:PassRole grant)."
}

variable "sanctions_bucket" {
  type        = string
  description = "Reference bucket the screener reads the sanctions watchlist bundle from."
}

variable "sanctions_bucket_arn" {
  type        = string
  description = "ARN of the reference bucket (scopes the s3:GetObject grant for the bundle)."
}

variable "health_schedule_enabled" {
  type        = bool
  default     = false
  description = "Enable the hourly connector health-probe EventBridge schedule. Off during the POC pilot — enable after confirming one manual probe invoke writes the health table."
}

variable "ab_runner_enabled" {
  type        = bool
  default     = false
  description = <<-EOT
    Create the temporary A/B model-measurement Lambda (aphrodite-ab-runner).
    Off by default — flip on to run the model comparison, then flip off. It reuses
    the search execution role, has no API Gateway route, and does not change the
    deployed synthesis model.
  EOT
}

variable "org_name" {
  type        = string
  default     = "Aphrodite"
  description = "Deployment's organization display name, used in the matter-search system prompt."
}

variable "org_office_address" {
  type        = string
  default     = "545 Clyde Avenue, West Vancouver"
  description = "Deployment's office address, used by the matter-search system prompt's care-of/mailing-address rule."
}

variable "excluded_sharepoint_folders" {
  type        = list(string)
  default     = ["hr", "human resources", "finance"]
  description = "SharePoint folder names excluded from search results. Case-insensitive."

  validation {
    condition     = alltrue([for f in var.excluded_sharepoint_folders : trimspace(f) != ""])
    error_message = "excluded_sharepoint_folders entries must not be blank (a blank entry would match every folder name)."
  }
}
