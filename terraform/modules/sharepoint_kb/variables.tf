variable "aws_region" {
  type        = string
  description = "AWS region. S3 Vectors + Bedrock KB are provisioned here (ca-central-1 supported → data stays in Canada)."
}

variable "source_dir" {
  type        = string
  description = "Directory containing the built Lambda package (code + deps) — shared with the other Lambdas."
}

variable "graph_secret_arn" {
  type        = string
  description = "Secrets Manager ARN of the Microsoft Graph app credentials the crawler reads."
}

variable "kms_key_arn" {
  type        = string
  description = "CMK ARN used to encrypt the crawl bucket + vector store and decrypt the Graph secret."
}

variable "sharepoint_sites" {
  type        = list(string)
  default     = []
  description = "SharePoint site URLs to crawl (each granted to the Graph app via Sites.Selected)."
}

variable "embedding_model_id" {
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
  description = "Bedrock embedding model for the KB. Titan Text Embeddings v2 (1024-dim) is on-demand in-region."
}

variable "embedding_dimensions" {
  type        = number
  default     = 1024
  description = "Embedding vector dimension. Must match the model + the S3 vector index (Titan v2 default 1024)."
}

variable "schedule_enabled" {
  type        = bool
  default     = false
  description = "Enable the nightly crawl schedule. Off by default — run the crawler manually for the pilot."
}

variable "schedule_expression" {
  type        = string
  default     = "cron(0 7 * * ? *)" # 07:00 UTC daily (overnight in America/Vancouver)
  description = "EventBridge Scheduler expression for the nightly crawl."
}

variable "schedule_timezone" {
  type        = string
  default     = "America/Vancouver"
  description = "Timezone for the crawl schedule."
}

variable "excluded_sharepoint_folders" {
  type        = list(string)
  default     = ["hr", "human resources", "finance"]
  description = "SharePoint folder names excluded from the crawl (and the search API's matching exclusion). Case-insensitive."
}
