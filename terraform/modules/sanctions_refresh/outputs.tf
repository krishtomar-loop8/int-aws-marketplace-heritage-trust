output "function_name" {
  description = "Sanctions-refresh Lambda name (for manual invoke / logs)."
  value       = aws_lambda_function.refresh.function_name
}

output "function_arn" {
  description = "Sanctions-refresh Lambda ARN."
  value       = aws_lambda_function.refresh.arn
}

output "schedule_name" {
  description = "EventBridge Scheduler schedule name."
  value       = aws_scheduler_schedule.refresh.name
}
