output "function_name" {
  description = "Quarterly report generator Lambda name (for manual invoke / logs)."
  value       = aws_lambda_function.quarterly.function_name
}

output "function_arn" {
  description = "Quarterly report generator Lambda ARN."
  value       = aws_lambda_function.quarterly.arn
}

output "notify_topic_arn" {
  description = "SNS topic ARN for 'quarterly drafts ready' staff notifications."
  value       = aws_sns_topic.notify.arn
}

output "schedule_name" {
  description = "EventBridge Scheduler schedule name."
  value       = aws_scheduler_schedule.quarterly.name
}
