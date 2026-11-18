output "subscriptions_table" {
  description = "DynamoDB table name holding pending-resolution (SESSION#) and subscriber (SUB#) records."
  value       = aws_dynamodb_table.subscriptions.name
}

output "api_url" {
  description = "Base URL of the fulfillment/subscribe HTTP API (custom domain)."
  value       = "https://${var.api_domain}"
}

output "site_url" {
  description = "Public URL of the static landing-page site (custom domain)."
  value       = "https://${var.site_domain}"
}

# -- extra outputs useful to Task 5's CI workflows, not strictly required by
# -- this task's own Interfaces but cheap to surface now.

output "site_bucket" {
  description = "S3 bucket CI syncs the static site build into."
  value       = aws_s3_bucket.site.id
}

output "site_distribution_id" {
  description = "CloudFront distribution id for the static site (for cache invalidation)."
  value       = aws_cloudfront_distribution.site.id
}

output "installer_mirror_bucket" {
  description = "S3 bucket the release workflow mirrors installer.yaml into."
  value       = aws_s3_bucket.installer_mirror.id
}

output "lambda_function_name" {
  description = "Name of the fulfillment/subscribe Lambda function (for CI code deploys)."
  value       = aws_lambda_function.api.function_name
}
