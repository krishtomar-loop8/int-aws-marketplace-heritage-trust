# SharePoint document Knowledge Base (full-body RAG) for the matter-search agent.
#
# App-only content *search* is blocked on HT's legacy NetDocs libraries, but app-
# only *reading* works. So a scheduled crawler copies documents to an S3 bucket
# and a Bedrock Knowledge Base indexes their full body text into an S3 Vectors
# store (cheapest vector store; GA in ca-central-1 → data stays in Canada). The
# search Lambda then runs a semantic Retrieve at query time.
#
# Requires the hashicorp/aws provider >= 6.24 (aws_s3vectors_* + the S3_VECTORS
# storage type). Not validatable without AWS creds — run `terraform validate` and
# `plan` on a logged-in session before the first apply.

data "aws_caller_identity" "current" {}

locals {
  crawler_function_name = "aphrodite-sharepoint-crawler"
  crawl_bucket_name     = "aphrodite-sharepoint-crawl-${data.aws_caller_identity.current.account_id}"
}

# -- crawl bucket (KB S3 data source) ---------------------------------------

resource "aws_s3_bucket" "crawl" {
  bucket = local.crawl_bucket_name
}

resource "aws_s3_bucket_versioning" "crawl" {
  bucket = aws_s3_bucket.crawl.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "crawl" {
  bucket = aws_s3_bucket.crawl.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "crawl" {
  bucket                  = aws_s3_bucket.crawl.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -- S3 Vectors store -------------------------------------------------------

resource "aws_s3vectors_vector_bucket" "vectors" {
  vector_bucket_name = "aphrodite-sharepoint-vectors-${data.aws_caller_identity.current.account_id}"

  encryption_configuration = [{
    sse_type    = "aws:kms"
    kms_key_arn = var.kms_key_arn
  }]
}

resource "aws_s3vectors_index" "docs" {
  index_name         = "sharepoint-docs"
  vector_bucket_name = aws_s3vectors_vector_bucket.vectors.vector_bucket_name
  data_type          = "float32"
  dimension          = var.embedding_dimensions
  distance_metric    = "cosine"

  # Bedrock stores the chunk text + its managed metadata as non-filterable keys;
  # these exact key names are required for the KB↔S3-Vectors integration.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

# -- Bedrock KB service role ------------------------------------------------

data "aws_iam_policy_document" "kb_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "kb" {
  name               = "aphrodite-sharepoint-kb"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.kb_assume.json
}

data "aws_iam_policy_document" "kb" {
  statement {
    sid       = "InvokeEmbeddingModel"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"]
  }
  statement {
    sid       = "ReadCrawlBucket"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.crawl.arn, "${aws_s3_bucket.crawl.arn}/*"]
  }
  statement {
    sid = "S3Vectors"
    actions = [
      "s3vectors:GetVectorBucket",
      "s3vectors:GetIndex",
      "s3vectors:ListVectors",
      "s3vectors:GetVectors",
      "s3vectors:PutVectors",
      "s3vectors:QueryVectors",
      "s3vectors:DeleteVectors",
    ]
    resources = [
      aws_s3vectors_vector_bucket.vectors.vector_bucket_arn,
      "${aws_s3vectors_vector_bucket.vectors.vector_bucket_arn}/*",
    ]
  }
  statement {
    sid       = "UseKms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "kb" {
  name   = "aphrodite-sharepoint-kb"
  role   = aws_iam_role.kb.id
  policy = data.aws_iam_policy_document.kb.json
}

# -- Knowledge Base + S3 data source ----------------------------------------

resource "aws_bedrockagent_knowledge_base" "docs" {
  name        = "aphrodite-sharepoint"
  role_arn    = aws_iam_role.kb.arn
  description = "HT SharePoint documents (full body text) for matter-search retrieval."

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = var.embedding_dimensions
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      vector_bucket_arn = aws_s3vectors_vector_bucket.vectors.vector_bucket_arn
      # The provider requires index_name (not index_arn) alongside vector_bucket_arn.
      index_name = aws_s3vectors_index.docs.index_name
    }
  }

  depends_on = [aws_iam_role_policy.kb]
}

resource "aws_bedrockagent_data_source" "crawl" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.docs.id
  name              = "sharepoint-crawl"
  # Keep vectors on `Delete` cleanup so tearing down the POC leaves nothing behind.
  data_deletion_policy = "DELETE"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.crawl.arn
      inclusion_prefixes = ["documents/"]
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "FIXED_SIZE"
      fixed_size_chunking_configuration {
        max_tokens         = 500
        overlap_percentage = 20
      }
    }
  }
}

# -- crawler Lambda ---------------------------------------------------------

data "archive_file" "crawler" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.root}/build/aphrodite-sharepoint-crawler.zip"
}

data "aws_iam_policy_document" "crawler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "crawler" {
  name               = "${local.crawler_function_name}-exec"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.crawler_assume.json
}

data "aws_iam_policy_document" "crawler" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
  statement {
    sid       = "ReadGraphSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.graph_secret_arn]
  }
  statement {
    sid       = "UseKms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  statement {
    sid       = "WriteCrawlBucket"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.crawl.arn, "${aws_s3_bucket.crawl.arn}/*"]
  }
  statement {
    sid       = "StartIngestion"
    actions   = ["bedrock:StartIngestionJob"]
    resources = [aws_bedrockagent_knowledge_base.docs.arn]
  }
  statement {
    # Self-invoke to continue a large backfill across invocations (the run stops
    # before the Lambda deadline with the cursor checkpointed). ARN built from the
    # known name to avoid a role→function→role dependency cycle.
    sid     = "SelfContinue"
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.crawler_function_name}"
    ]
  }
}

resource "aws_iam_role_policy" "crawler" {
  name   = local.crawler_function_name
  role   = aws_iam_role.crawler.id
  policy = data.aws_iam_policy_document.crawler.json
}

resource "aws_cloudwatch_log_group" "crawler" {
  name              = "/aws/lambda/${local.crawler_function_name}"
  retention_in_days = 90
}

resource "aws_lambda_function" "crawler" {
  function_name    = local.crawler_function_name
  role             = aws_iam_role.crawler.arn
  runtime          = "python3.13"
  handler          = "backend.lambdas.sharepoint_crawler.handler.handler"
  filename         = data.archive_file.crawler.output_path
  source_code_hash = data.archive_file.crawler.output_base64sha256
  timeout          = 900  # a full backfill of the archives can run long
  memory_size      = 2048 # more CPU/network for concurrent per-page downloads

  environment {
    variables = {
      APHRODITE_REGION                    = var.aws_region
      APHRODITE_KB_REGION                 = var.aws_region
      APHRODITE_SHAREPOINT_SITES          = jsonencode(var.sharepoint_sites)
      APHRODITE_CRAWL_BUCKET              = aws_s3_bucket.crawl.bucket
      APHRODITE_CRAWL_KMS_KEY_ID          = var.kms_key_arn
      APHRODITE_SELF_FUNCTION             = local.crawler_function_name
      APHRODITE_SHAREPOINT_KB_ID          = aws_bedrockagent_knowledge_base.docs.id
      APHRODITE_SHAREPOINT_DATA_SOURCE_ID = aws_bedrockagent_data_source.crawl.data_source_id
    }
  }

  depends_on = [aws_cloudwatch_log_group.crawler, aws_iam_role_policy.crawler]
}

# The crawler intentionally self-invokes (handler._self_continue, async Event, up
# to _MAX_CONTINUES=200) to drain the large legacy libraries across many 900s
# runs. Lambda's recursive-loop detection otherwise flags that chain (~16+ hops)
# as a runaway and drops it (AWS_LAMBDA_RUNAWAY_TERMINATION_NOTIFICATION), cutting
# the backfill short. "Allow" opts this function out; the self-continue's own
# bounds — the 200-hop cap + a forward-progress gate (only continues when a page
# was processed) — remain the guardrails against a true runaway.
resource "aws_lambda_function_recursion_config" "crawler" {
  function_name  = aws_lambda_function.crawler.function_name
  recursive_loop = "Allow"
}

# -- nightly schedule (disabled by default) ---------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.crawler_function_name}-scheduler"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-sharepoint-crawler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.crawler.arn
    }]
  })
}

resource "aws_scheduler_schedule" "crawl" {
  name       = local.crawler_function_name
  group_name = "default"
  state      = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  target {
    arn      = aws_lambda_function.crawler.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }
}
