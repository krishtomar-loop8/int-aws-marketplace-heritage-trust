data "aws_caller_identity" "current" {}

# Phase 1 foundation (D1) + Cognito SSO shell (part of D4).
module "kms" {
  source = "./modules/kms"
}

module "secrets" {
  source      = "./modules/secrets"
  kms_key_arn = module.kms.secrets_key_arn
}

module "iam" {
  source      = "./modules/iam"
  secret_arns = values(module.secrets.secret_arns)
  kms_key_arn = module.kms.secrets_key_arn
}

module "logging" {
  source     = "./modules/logging"
  aws_region = var.aws_region
}

# Phase 2 (D4): SPA hosting — private S3 origin + CloudFront (+ optional custom domain).
module "web" {
  source = "./modules/web"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }
  custom_domain        = var.custom_domain
  enable_custom_domain = var.enable_custom_domain
}

locals {
  # Every origin the SPA is served from — always CloudFront, plus the custom
  # domain once it's live. Used for Cognito OAuth callbacks and API CORS.
  web_origins = compact([
    module.web.cloudfront_url,
    var.enable_custom_domain ? "https://${var.custom_domain}" : "",
  ])
}

module "cognito" {
  source            = "./modules/cognito"
  aws_region        = var.aws_region
  saml_metadata_url = var.saml_metadata_url
  # Register every SPA origin as an OAuth callback (keep localhost for dev).
  callback_urls = concat(["https://localhost:3000/callback"], [for u in local.web_origins : "${u}/"])
  logout_urls   = concat(["https://localhost:3000/logout"], [for u in local.web_origins : "${u}/"])
}

# Phase 2 (D3/D4): Global Matter Search API — orchestrator Lambda + HTTP API
# (Cognito JWT authorizer) + async job store. Reads the connector secrets and
# invokes Bedrock.
module "search_api" {
  source                  = "./modules/search_api"
  aws_region              = var.aws_region
  source_dir              = "${path.root}/build/lambda"
  secret_arns             = values(module.secrets.secret_arns)
  quickbooks_secret_arn   = module.secrets.secret_arns["quickbooks"]
  clio_secret_arn         = module.secrets.secret_arns["clio"]
  kms_key_arn             = module.kms.secrets_key_arn
  cognito_user_pool_id    = module.cognito.user_pool_id
  cognito_app_client_id   = module.cognito.user_pool_client_id
  allowed_origins         = concat(local.web_origins, ["http://localhost:5173"])
  outputs_bucket          = module.outputs_bucket.bucket
  outputs_bucket_arn      = module.outputs_bucket.bucket_arn
  quarterly_function_name = module.quarterly_report.function_name
  quarterly_function_arn  = module.quarterly_report.function_arn
  # AML Export tab: on-demand "run now" (invoke) + self-serve monthly schedule.
  aml_export_function_name = module.aml_export.function_name
  aml_export_function_arn  = module.aml_export.function_arn
  aml_schedule_name        = module.aml_export.schedule_name
  aml_schedule_arn         = module.aml_export.schedule_arn
  aml_scheduler_role_arn   = module.aml_export.scheduler_role_arn
  # SharePoint documents are served from a Bedrock Knowledge Base (full-body index
  # built by the crawler) — app-only live search is blocked on HT's legacy NetDocs
  # libraries, so we index + retrieve instead. See module.sharepoint_kb.
  sharepoint_kb_id  = module.sharepoint_kb.knowledge_base_id
  sharepoint_kb_arn = module.sharepoint_kb.knowledge_base_arn
  # Sanction Screener reads the watchlist bundle from the reference bucket and
  # writes Certificates of Search to the outputs bucket under screener/.
  sanctions_bucket     = module.reference_bucket.bucket
  sanctions_bucket_arn = module.reference_bucket.bucket_arn
  # Hourly connector health probe backing the Integrations tab (off by default).
  health_schedule_enabled = var.integration_health_schedule_enabled
  # Temporary A/B model-measurement Lambda (off by default).
  ab_runner_enabled = var.ab_runner_enabled
}

# Phase 2+ (D3): SharePoint document Knowledge Base — a scheduled crawler copies
# documents from the granted SharePoint sites to S3 (app-only reading works even
# where live search is blocked), a Bedrock KB indexes their full body text into an
# S3 Vectors store (GA in ca-central-1 → data stays in Canada), and the search
# Lambda retrieves semantically at query time. Crawl schedule off until the site
# set is confirmed. See backend/lambdas/sharepoint_crawler.
module "sharepoint_kb" {
  source           = "./modules/sharepoint_kb"
  aws_region       = var.aws_region
  source_dir       = "${path.root}/build/lambda"
  graph_secret_arn = module.secrets.secret_arns["graph"]
  kms_key_arn      = module.kms.secrets_key_arn
  sharepoint_sites = var.sharepoint_sites
  schedule_enabled = var.sharepoint_crawl_schedule_enabled
}

# Phase 4 (D7): immutable outputs bucket for generated artifacts. Used by the
# quarterly report generator (D5) now; the AML export (D6) will reuse it.
module "outputs_bucket" {
  source      = "./modules/outputs_bucket"
  kms_key_arn = module.kms.secrets_key_arn
}

# Phase 3 (D5): Quarterly Report Draft Generator — scheduled/manual Lambda that
# writes per-matter trust statement drafts (.docx + .pdf) to the outputs bucket
# for human review in the portal. Reads Clio (trust ledger) only.
module "quarterly_report" {
  source               = "./modules/quarterly_report"
  aws_region           = var.aws_region
  source_dir           = "${path.root}/build/lambda"
  secret_arns          = values(module.secrets.secret_arns)
  clio_secret_arn      = module.secrets.secret_arns["clio"]
  kms_key_arn          = module.kms.secrets_key_arn
  outputs_bucket       = module.outputs_bucket.bucket
  outputs_bucket_arn   = module.outputs_bucket.bucket_arn
  reference_bucket     = module.reference_bucket.bucket
  reference_bucket_arn = module.reference_bucket.bucket_arn
  matter_ids           = var.quarterly_matter_ids
  notify_emails        = var.quarterly_notify_emails
  schedule_enabled     = var.quarterly_schedule_enabled
  # Practice-area discovery for scheduled runs (Nicole 2026-08-06).
  qualifying_practice_areas = var.quarterly_qualifying_practice_areas
  exclusion_field           = var.quarterly_exclusion_field
  # Address beneficiaries from Estateably (Nicole 2026-08-06).
  beneficiary_emails_enabled = var.quarterly_beneficiary_emails_enabled
  # SharePoint delivery on scheduled runs (off until the Graph write grant lands).
  sharepoint_upload_enabled = var.quarterly_sharepoint_upload_enabled
  sharepoint_upload_site    = var.quarterly_sharepoint_upload_site
  sharepoint_upload_folder  = var.quarterly_sharepoint_upload_folder
}

# Phase 3 (D6): Monthly AML Name Export — scheduled Lambda that compiles the
# active-client roster (Estateably + Clio) into the Estates/Trusts/POA workbook
# for staff to screen in the in-app Sanction Screener. Never calls Bedrock.
module "aml_export" {
  source               = "./modules/aml_export"
  aws_region           = var.aws_region
  source_dir           = "${path.root}/build/lambda"
  secret_arns          = values(module.secrets.secret_arns)
  clio_secret_arn      = module.secrets.secret_arns["clio"]
  kms_key_arn          = module.kms.secrets_key_arn
  outputs_bucket       = module.outputs_bucket.bucket
  outputs_bucket_arn   = module.outputs_bucket.bucket_arn
  reference_bucket     = module.reference_bucket.bucket
  reference_bucket_arn = module.reference_bucket.bucket_arn
  notify_emails        = var.aml_export_notify_emails
  schedule_enabled     = var.aml_export_schedule_enabled
}

# Sanction Screener companion — brings HT's custom screening tool (originally
# contracted to WorkSmarter) in-house as its replacement; the old hosted URL is
# dead. A mutable reference bucket holds the watchlist bundle, refreshed monthly
# from the public UN/SEMA/Public-Safety sources by the sanctions-refresh Lambda;
# the search API Lambda reads it to screen uploaded name lists and writes
# Certificates of Search to the outputs bucket. Never calls Bedrock.
module "reference_bucket" {
  source      = "./modules/reference_bucket"
  kms_key_arn = module.kms.secrets_key_arn
}

module "sanctions_refresh" {
  source               = "./modules/sanctions_refresh"
  aws_region           = var.aws_region
  source_dir           = "${path.root}/build/lambda"
  reference_bucket     = module.reference_bucket.bucket
  reference_bucket_arn = module.reference_bucket.bucket_arn
  kms_key_arn          = module.kms.secrets_key_arn
  schedule_enabled     = var.sanctions_refresh_schedule_enabled
}
