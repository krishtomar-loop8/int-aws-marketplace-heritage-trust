data "aws_caller_identity" "current" {}

locals {
  function_name = "aphrodite-search"
  # Self-invoke ARN built from the (known) name to avoid a role→function→role cycle.
  function_arn = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.function_name}"
}

# -- job store --------------------------------------------------------------

resource "aws_dynamodb_table" "jobs" {
  name         = "aphrodite-search-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  point_in_time_recovery {
    enabled = true
  }

  attribute {
    name = "job_id"
    type = "S"
  }

  # GSI keys for the per-user history list (newest-first): partition by the
  # Cognito sub, sort by created_at. Only rows that carry both attributes are
  # indexed (all new jobs do).
  attribute {
    name = "actor_sub"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "N"
  }

  global_secondary_index {
    name      = "by-actor"
    hash_key  = "actor_sub"
    range_key = "created_at"
    # List view needs query/matter_name/status/phase (+ keys) — not the large
    # result blob, which is fetched on demand via GetItem when a search is re-opened.
    projection_type    = "INCLUDE"
    non_key_attributes = ["query", "matter_name", "status", "phase"]
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Status files (client PII) now persist ~30 days to back history → encrypt at
  # rest with the project CMK rather than the AWS-owned key.
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
}

# -- activity log (chain-of-custody audit trail) ----------------------------
# Append-only: one partition ("activity"), sortable range key so the whole log
# reads newest-first in a single Query. No TTL — retained for audit. The Lambda
# role is granted PutItem + Query only (see the ActivityLog IAM statement), so
# rows cannot be altered or removed through the app.
resource "aws_dynamodb_table" "activity" {
  name         = "aphrodite-activity-log"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "log_id"
  range_key    = "ts"

  point_in_time_recovery {
    enabled = true
  }

  attribute {
    name = "log_id"
    type = "S"
  }

  attribute {
    name = "ts"
    type = "S"
  }
}

# -- integration health (scheduled connector probe) -------------------------
# One partition ("health"): a mutable current row per integration (sk "cur#<name>")
# plus append-only transition rows (sk "log#<ISO>#<uuid>") for the downtime log. The
# hourly health probe (the Lambda's health_probe path) writes; GET /integrations
# reads. SSE-KMS with the project CMK (the detail can carry a connector error string).
resource "aws_dynamodb_table" "integration_health" {
  name         = "aphrodite-integration-health"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  point_in_time_recovery {
    enabled = true
  }

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
}

# -- lambda package ---------------------------------------------------------

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.root}/build/aphrodite-search.zip"
}

# -- execution role ---------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "search" {
  name               = "aphrodite-search-exec"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "search" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
  statement {
    sid       = "ReadSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.secret_arns
  }
  statement {
    # QuickBooks rotates its OAuth refresh token on every refresh and invalidates
    # the prior one; the connector persists the rotated value so it survives cold
    # starts. Write scoped to the QuickBooks secret only.
    sid       = "WriteQuickBooksToken"
    actions   = ["secretsmanager:PutSecretValue"]
    resources = [var.quickbooks_secret_arn]
  }
  statement {
    # Clio likewise rotates its OAuth refresh token on refresh; the connector
    # persists the rotated value so it survives cold starts (without this the
    # stored token goes stale and paginated Clio calls 401). Scoped to Clio only.
    sid       = "WriteClioToken"
    actions   = ["secretsmanager:PutSecretValue"]
    resources = [var.clio_secret_arn]
  }
  statement {
    # Decrypt the connector secrets and the KMS-encrypted objects we read, and
    # generate a data key to write the SSE-KMS Certificate of Search.
    sid       = "UseKms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  statement {
    sid     = "InvokeBedrock"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    # Cross-region inference profile + the underlying Anthropic foundation models.
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    ]
  }
  # SharePoint document retrieval from the Bedrock Knowledge Base (full-body RAG).
  # Only granted when a KB is wired in.
  dynamic "statement" {
    for_each = var.sharepoint_kb_arn != "" ? [1] : []
    content {
      sid       = "RetrieveKnowledgeBase"
      actions   = ["bedrock:Retrieve"]
      resources = [var.sharepoint_kb_arn]
    }
  }
  statement {
    sid = "JobStore"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", # staff can clear a stale/errored search from their history
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.jobs.arn, "${aws_dynamodb_table.jobs.arn}/index/*"]
  }
  # Chain-of-custody activity log: append + read only. No DeleteItem/UpdateItem,
  # so audit rows are immutable from the application's side.
  statement {
    sid       = "ActivityLog"
    actions   = ["dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.activity.arn]
  }
  # Integration health: the scheduled probe reads the prior state (GetItem) and
  # writes the current row + transition log (PutItem); GET /integrations reads
  # (Query). No DeleteItem — the downtime log is append-only.
  statement {
    sid       = "IntegrationHealth"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.integration_health.arn]
  }
  statement {
    sid       = "SelfInvokeWorker"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.function_arn]
  }
  # Read-only access to the quarterly report drafts for the /reports routes
  # (list manifests + presign downloads). KMS decrypt is already granted above
  # (same CMK encrypts the outputs bucket).
  statement {
    sid       = "ListReports"
    actions   = ["s3:ListBucket"]
    resources = [var.outputs_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["quarterly/*", "aml/*", "screener/*"]
    }
  }
  statement {
    sid       = "ReadReports"
    actions   = ["s3:GetObject"]
    resources = ["${var.outputs_bucket_arn}/quarterly/*"]
  }
  # Same read-only shape for the AML export workbooks (D6) backing the
  # /aml-exports routes.
  statement {
    sid       = "ReadAmlExports"
    actions   = ["s3:GetObject"]
    resources = ["${var.outputs_bucket_arn}/aml/*"]
  }
  # Sanction Screener: read the watchlist bundle from the reference bucket, and
  # read/write Certificates of Search + manifests under screener/ in the
  # outputs bucket (backing POST /screener/screen, /screener/certificate, and
  # the /screener/certificates list + download routes).
  statement {
    sid       = "ReadSanctionsBundle"
    actions   = ["s3:GetObject"]
    resources = ["${var.sanctions_bucket_arn}/sanctions/*"]
  }
  statement {
    sid       = "WriteScreenerCertificates"
    actions   = ["s3:PutObject"]
    resources = ["${var.outputs_bucket_arn}/screener/*"]
  }
  statement {
    sid       = "ReadScreenerCertificates"
    actions   = ["s3:GetObject"]
    resources = ["${var.outputs_bucket_arn}/screener/*"]
  }
  # Soft-delete a certificate: DeleteObject (no version) on a versioned+Object-Lock
  # bucket writes a delete marker, hiding it from the list while the locked version is
  # retained. No BypassGovernanceRetention / DeleteObjectVersion — the audit copy stays.
  statement {
    sid       = "DeleteScreenerCertificates"
    actions   = ["s3:DeleteObject"]
    resources = ["${var.outputs_bucket_arn}/screener/*"]
  }
  # On-demand quarterly generation: the portal triggers the quarterly-report
  # Lambda (which does the Clio reads + S3 write) via POST /reports/generate.
  statement {
    sid       = "InvokeQuarterly"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.quarterly_function_arn]
  }
  # On-demand AML export "run now": the portal invokes the AML export Lambda.
  statement {
    sid       = "InvokeAmlExport"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.aml_export_function_arn]
  }
  # Self-serve AML schedule: read + update the EventBridge schedule, and pass the
  # scheduler's role on update (scoped to that one role + the scheduler service).
  statement {
    sid       = "ManageAmlSchedule"
    actions   = ["scheduler:GetSchedule", "scheduler:UpdateSchedule"]
    resources = [var.aml_schedule_arn]
  }
  statement {
    sid       = "PassSchedulerRole"
    actions   = ["iam:PassRole"]
    resources = [var.aml_scheduler_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "search" {
  name   = "aphrodite-search"
  role   = aws_iam_role.search.id
  policy = data.aws_iam_policy_document.search.json
}

# -- lambda -----------------------------------------------------------------

resource "aws_cloudwatch_log_group" "search" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 90
}

resource "aws_lambda_function" "search" {
  function_name    = local.function_name
  role             = aws_iam_role.search.arn
  runtime          = "python3.13"
  handler          = "backend.api.handlers.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  # The async search worker fans out to every source (Estateably, Clio — the
  # 50 req/min bottleneck — Outlook full-body, SharePoint KB) then runs a large
  # Bedrock synthesis; a heavy matter (e.g. a litigation estate) can take ~3.5 min,
  # and more under Clio rate-limit contention, plus a possible one-shot synthesis
  # retry (~450s read timeout each). Accuracy-first, so give the worker generous
  # headroom — the synthesizer skips its retry when the remaining budget is short
  # (backend/search/synthesize.py) so it can't run past this ceiling and strand a
  # job. The API-facing POST/GET calls still return in <1s; billed per actual ms,
  # so the higher ceiling costs nothing on fast searches.
  timeout     = 900
  memory_size = 1024

  environment {
    variables = {
      APHRODITE_REGION                   = var.aws_region
      APHRODITE_JOBS_TABLE               = aws_dynamodb_table.jobs.name
      APHRODITE_ACTIVITY_TABLE           = aws_dynamodb_table.activity.name
      APHRODITE_INTEGRATION_HEALTH_TABLE = aws_dynamodb_table.integration_health.name
      APHRODITE_SELF_FUNCTION            = local.function_name
      APHRODITE_SHAREPOINT_KB_ID         = var.sharepoint_kb_id
      APHRODITE_OUTPUTS_BUCKET           = var.outputs_bucket
      APHRODITE_OUTPUTS_KMS_KEY_ID       = var.kms_key_arn
      APHRODITE_QUARTERLY_FUNCTION       = var.quarterly_function_name
      APHRODITE_SANCTIONS_BUCKET         = var.sanctions_bucket
      APHRODITE_AML_EXPORT_FUNCTION      = var.aml_export_function_name
      APHRODITE_AML_SCHEDULE_NAME        = var.aml_schedule_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.search, aws_iam_role_policy.search]
}

# -- A/B model measurement runner (temporary) -------------------------------
# A throwaway Lambda that drives the SAME search pipeline against a candidate
# Bedrock model, for the model comparison the client requested. Created only when
# ab_runner_enabled = true; it has NO API Gateway route (invoked out-of-band via
# `aws lambda invoke`), reuses the search execution role (Bedrock/KB/Secrets/KMS),
# and never changes the deployed synthesis model. Turn off after the test.
resource "aws_cloudwatch_log_group" "ab_runner" {
  count             = var.ab_runner_enabled ? 1 : 0
  name              = "/aws/lambda/aphrodite-ab-runner"
  retention_in_days = 30
}

resource "aws_lambda_function" "ab_runner" {
  count            = var.ab_runner_enabled ? 1 : 0
  function_name    = "aphrodite-ab-runner"
  role             = aws_iam_role.search.arn
  runtime          = "python3.13"
  handler          = "backend.lambdas.ab_runner.handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  # One full search (heavy matter fan-out + large synthesis) per invocation; the
  # operator loops matters × models from the CLI. Same headroom as the worker.
  timeout     = 900
  memory_size = 1024

  environment {
    variables = {
      APHRODITE_REGION             = var.aws_region
      APHRODITE_JOBS_TABLE         = aws_dynamodb_table.jobs.name
      APHRODITE_ACTIVITY_TABLE     = aws_dynamodb_table.activity.name
      APHRODITE_SELF_FUNCTION      = local.function_name
      APHRODITE_SHAREPOINT_KB_ID   = var.sharepoint_kb_id
      APHRODITE_OUTPUTS_BUCKET     = var.outputs_bucket
      APHRODITE_OUTPUTS_KMS_KEY_ID = var.kms_key_arn
    }
  }

  depends_on = [aws_iam_role_policy.search]
}

# -- HTTP API + Cognito JWT authorizer --------------------------------------

resource "aws_apigatewayv2_api" "search" {
  name          = "aphrodite-search"
  protocol_type = "HTTP"

  # Browser SPA calls this cross-origin (CloudFront → execute-api), so the API
  # must answer CORS preflight. API Gateway handles OPTIONS automatically and
  # preflight bypasses the JWT authorizer.
  cors_configuration {
    allow_origins = var.allowed_origins
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["authorization", "content-type", "x-file-name"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.search.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"

  jwt_configuration {
    audience = [var.cognito_app_client_id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${var.cognito_user_pool_id}"
  }
}

resource "aws_apigatewayv2_integration" "search" {
  api_id                 = aws_apigatewayv2_api.search.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.search.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "routes" {
  for_each = toset([
    "POST /resolve",
    "POST /search",
    "GET /search",
    "GET /search/{jobId}",
    "DELETE /search/{jobId}",
    "GET /reports",
    "GET /reports/download",
    "GET /aml-exports",
    "GET /aml-exports/download",
    "GET /matters",
    "POST /reports/generate",
    "POST /aml-exports/generate",
    "GET /aml-exports/schedule",
    "PUT /aml-exports/schedule",
    "POST /screener/screen",
    "POST /screener/certificate",
    "GET /screener/certificates",
    "GET /screener/certificates/download",
    "DELETE /screener/certificates",
    "GET /activity",
    "GET /integrations",
  ])

  api_id             = aws_apigatewayv2_api.search.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.search.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.search.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.search.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.search.execution_arn}/*/*"
}

# -- EventBridge Scheduler: hourly connector health probe -------------------
# Invokes this Lambda with {"health_probe": true} so the D2 connectivity harness
# runs read-only against every connector and records status (backing the
# Integrations tab). Catches a silently-dead connector (e.g. an expired
# QuickBooks token) between searches. Uses the scheduler's own role — EventBridge
# Scheduler invokes via role_arn, so no aws_lambda_permission is needed.

data "aws_iam_policy_document" "health_scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "health_scheduler" {
  name               = "aphrodite-integration-health-scheduler"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.health_scheduler_assume.json
}

resource "aws_iam_role_policy" "health_scheduler" {
  name = "invoke-search-health-probe"
  role = aws_iam_role.health_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.search.arn
    }]
  })
}

# Disabled by default for the POC: enable via var.health_schedule_enabled once a
# manual probe invoke is confirmed to write the health table. Fixed hourly cadence.
resource "aws_scheduler_schedule" "integration_health" {
  name       = "aphrodite-integration-health"
  group_name = "default"
  state      = var.health_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "rate(1 hour)"

  target {
    arn      = aws_lambda_function.search.arn
    role_arn = aws_iam_role.health_scheduler.arn
    input    = jsonencode({ health_probe = true })
  }
}
