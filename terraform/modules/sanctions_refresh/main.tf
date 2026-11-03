data "aws_caller_identity" "current" {}

locals {
  function_name = "aphrodite-sanctions-refresh"
}

# -- lambda package (same build dir as the other Lambdas) -------------------

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.root}/build/aphrodite-sanctions-refresh.zip"
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

resource "aws_iam_role" "exec" {
  name               = "aphrodite-sanctions-refresh-exec"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "exec" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
  # Write the refreshed watchlist bundle (SSE-KMS) to the reference bucket.
  statement {
    sid       = "WriteBundle"
    actions   = ["s3:PutObject"]
    resources = ["${var.reference_bucket_arn}/sanctions/*"]
  }
  statement {
    sid       = "UseKms"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "exec" {
  name   = "aphrodite-sanctions-refresh"
  role   = aws_iam_role.exec.id
  policy = data.aws_iam_policy_document.exec.json
}

# -- lambda -----------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 90
}

resource "aws_lambda_function" "refresh" {
  function_name    = local.function_name
  role             = aws_iam_role.exec.arn
  runtime          = "python3.13"
  handler          = "backend.lambdas.sanctions_refresh.handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300
  memory_size      = 1024

  environment {
    variables = {
      APHRODITE_REGION               = var.aws_region
      APHRODITE_SANCTIONS_BUCKET     = var.reference_bucket
      APHRODITE_SANCTIONS_KEY        = "sanctions/bundle.json"
      APHRODITE_SANCTIONS_KMS_KEY_ID = var.kms_key_arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.exec]
}

# -- EventBridge Scheduler (monthly) -----------------------------------------

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
  name               = "aphrodite-sanctions-refresh-scheduler"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-sanctions-refresh"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.refresh.arn
    }]
  })
}

# Monthly refresh of the sanctions bundle. Enabled by default (unlike the AML /
# quarterly schedules): this job only writes public reference data to S3 — no
# staff-facing artifact, no external side effect — and current watchlists are
# the whole point. Toggle off with var.schedule_enabled if needed.
resource "aws_scheduler_schedule" "refresh" {
  name       = "aphrodite-sanctions-refresh"
  group_name = "default"
  state      = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  target {
    arn      = aws_lambda_function.refresh.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }
}
