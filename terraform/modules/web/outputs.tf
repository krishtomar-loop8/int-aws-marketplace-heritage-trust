output "bucket" {
  description = "SPA origin S3 bucket name (CI syncs the build here)."
  value       = aws_s3_bucket.web.id
}

output "cloudfront_domain" {
  description = "CloudFront distribution domain name."
  value       = aws_cloudfront_distribution.web.domain_name
}

output "cloudfront_url" {
  description = "SPA base URL (https, no trailing slash)."
  value       = "https://${aws_cloudfront_distribution.web.domain_name}"
}

output "distribution_id" {
  description = "CloudFront distribution id (for cache invalidation)."
  value       = aws_cloudfront_distribution.web.id
}

# DNS records the customer must create to validate the ACM certificate. Empty until
# `custom_domain` is set. Surfaced up to the root `acm_validation_records` output.
output "acm_validation_records" {
  description = "ACM DNS validation records (CNAMEs) for HT to add in their DNS."
  value = var.custom_domain == "" ? [] : [
    for o in aws_acm_certificate.web[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ]
}

# The canonical URL: the custom domain once live, else the CloudFront default.
# The CI pipeline reads this to build config.json (SPA redirectUri) and Cognito
# uses it for OAuth callbacks.
output "primary_url" {
  description = "Canonical SPA URL (custom domain when enabled, else CloudFront)."
  value       = var.enable_custom_domain ? "https://${var.custom_domain}" : "https://${aws_cloudfront_distribution.web.domain_name}"
}
