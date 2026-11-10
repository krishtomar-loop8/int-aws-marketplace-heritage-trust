output "account_id" {
  description = "AWS account the stack is deployed to."
  value       = data.aws_caller_identity.current.account_id
}

output "deploy_identity" {
  description = "IAM identity Terraform is running as (the GitHub Actions deploy role)."
  value       = data.aws_caller_identity.current.arn
}

output "secrets_kms_key_arn" {
  description = "CMK encrypting the connector secrets."
  value       = module.kms.secrets_key_arn
}

output "connector_role_arn" {
  description = "Execution role for the connector Lambdas."
  value       = module.iam.connector_role_arn
}

output "cognito_user_pool_id" {
  description = "Cognito user pool ID."
  value       = module.cognito.user_pool_id
}

output "cognito_app_client_id" {
  description = "Cognito web app client ID (used in the SSO test authorize URL)."
  value       = module.cognito.user_pool_client_id
}

output "saml_sp_entity_id" {
  description = "SAML SP Entity ID (Identifier) to register in the Entra enterprise app."
  value       = module.cognito.saml_sp_entity_id
}

output "saml_acs_url" {
  description = "SAML ACS / Reply URL to register in the Entra enterprise app."
  value       = module.cognito.saml_acs_url
}

output "cloudtrail_bucket" {
  description = "CloudTrail delivery bucket."
  value       = module.logging.trail_bucket
}

output "search_api_endpoint" {
  description = "Base URL of the Global Matter Search HTTP API."
  value       = module.search_api.api_endpoint
}

output "web_bucket" {
  description = "SPA origin S3 bucket (CI syncs the build here)."
  value       = module.web.bucket
}

output "web_url" {
  description = "Canonical SPA base URL (custom domain when live, else CloudFront). CI builds config.json from this."
  value       = module.web.primary_url
}

output "cloudfront_url" {
  description = "CloudFront default SPA URL (always valid, even before the custom domain is live)."
  value       = module.web.cloudfront_url
}

output "acm_validation_records" {
  description = "DNS records to add to validate the custom-domain ACM certificate (when custom_domain is set)."
  value       = module.web.acm_validation_records
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution id (cache invalidation)."
  value       = module.web.distribution_id
}

output "cognito_authority" {
  description = "Cognito OIDC issuer/authority for the SPA."
  value       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}"
}

output "outputs_bucket" {
  description = "Immutable outputs bucket (quarterly report drafts, AML exports)."
  value       = module.outputs_bucket.bucket
}

output "quarterly_report_function" {
  description = "Quarterly report generator Lambda name (manual invoke for the pilot). Null when quarterly_enabled is false."
  value       = var.quarterly_enabled ? module.quarterly_report[0].function_name : null
}

output "reference_bucket" {
  description = "Reference-data bucket holding the sanctions watchlist bundle."
  value       = module.reference_bucket.bucket
}

output "sanctions_refresh_function" {
  description = "Sanctions-refresh Lambda name (manual invoke to seed/refresh the watchlist bundle). Null when sanction_screener_enabled is false."
  value       = var.sanction_screener_enabled ? module.sanctions_refresh[0].function_name : null
}
