output "bucket" {
  description = "Name of the immutable outputs bucket."
  value       = aws_s3_bucket.outputs.id
}

output "bucket_arn" {
  description = "ARN of the immutable outputs bucket."
  value       = aws_s3_bucket.outputs.arn
}
