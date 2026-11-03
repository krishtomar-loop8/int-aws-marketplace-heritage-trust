output "secret_arns" {
  description = "Map of source name to Secrets Manager secret ARN."
  value       = { for k, s in aws_secretsmanager_secret.source : k => s.arn }
}
