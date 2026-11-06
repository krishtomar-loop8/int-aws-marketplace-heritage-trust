output "api_endpoint" {
  description = "Base URL of the matter-search HTTP API."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "function_name" {
  description = "Search Lambda function name."
  value       = aws_lambda_function.search.function_name
}

output "jobs_table" {
  description = "DynamoDB jobs table name."
  value       = aws_dynamodb_table.jobs.name
}

output "integrations_alert_topic_arn" {
  description = "SNS topic ARN for the 'connector down' alert (same pattern as quarterly_report's notify_topic_arn)."
  value       = aws_sns_topic.integrations_alert.arn
}
