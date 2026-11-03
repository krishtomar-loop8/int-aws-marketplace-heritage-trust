output "connector_role_arn" {
  description = "ARN of the connector Lambda execution role."
  value       = aws_iam_role.connector.arn
}
