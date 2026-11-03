output "knowledge_base_id" {
  description = "Bedrock Knowledge Base id the search Lambda retrieves from."
  value       = aws_bedrockagent_knowledge_base.docs.id
}

output "knowledge_base_arn" {
  description = "Bedrock Knowledge Base ARN (scopes the search Lambda's bedrock:Retrieve grant)."
  value       = aws_bedrockagent_knowledge_base.docs.arn
}

output "data_source_id" {
  description = "KB S3 data source id (crawler triggers ingestion on it)."
  value       = aws_bedrockagent_data_source.crawl.data_source_id
}

output "crawl_bucket" {
  description = "S3 bucket the crawler writes documents to."
  value       = aws_s3_bucket.crawl.bucket
}

output "crawler_function_name" {
  description = "SharePoint crawler Lambda name."
  value       = aws_lambda_function.crawler.function_name
}
