output "secrets_key_arn" {
  description = "ARN of the Secrets Manager CMK."
  value       = aws_kms_key.secrets.arn
}
