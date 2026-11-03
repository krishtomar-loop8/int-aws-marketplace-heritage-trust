output "bucket" {
  description = "Name of the reference-data bucket."
  value       = aws_s3_bucket.reference.id
}

output "bucket_arn" {
  description = "ARN of the reference-data bucket."
  value       = aws_s3_bucket.reference.arn
}
