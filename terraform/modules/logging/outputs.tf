output "trail_bucket" {
  description = "CloudTrail delivery bucket name."
  value       = aws_s3_bucket.trail.id
}

output "app_log_group" {
  description = "Application CloudWatch log group name."
  value       = aws_cloudwatch_log_group.app.name
}
