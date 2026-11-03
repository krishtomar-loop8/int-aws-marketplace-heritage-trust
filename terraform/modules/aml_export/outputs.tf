output "function_name" {
  description = "AML export Lambda name (for manual invoke / logs)."
  value       = aws_lambda_function.aml_export.function_name
}

output "function_arn" {
  description = "AML export Lambda ARN."
  value       = aws_lambda_function.aml_export.arn
}

output "notify_topic_arn" {
  description = "SNS topic ARN for 'AML export ready' staff notifications."
  value       = aws_sns_topic.notify.arn
}

output "schedule_name" {
  description = "EventBridge Scheduler schedule name."
  value       = aws_scheduler_schedule.aml_export.name
}

output "schedule_arn" {
  description = "AML export EventBridge schedule ARN (scopes the portal's Get/UpdateSchedule grant)."
  value       = aws_scheduler_schedule.aml_export.arn
}

output "scheduler_role_arn" {
  description = "Role EventBridge Scheduler assumes to invoke the AML export (PassRole target)."
  value       = aws_iam_role.scheduler.arn
}
