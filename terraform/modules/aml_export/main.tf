data "aws_caller_identity" "current" {}

locals {
  function_name = "aphrodite-aml-export"
}

# -- lambda package (same build dir as the other Lambdas) -------------------

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.root}/build/aphrodite-aml-export.zip"
}

# -- staff notification topic ("export ready for review") -------------------

resource "aws_sns_topic" "notify" {
  name = "aphrodite-aml-export-notify"
}

resource "aws_sns_topic_subscription" "emails" {
  for_each  = toset(var.notify_emails)
  topic_arn = aws_sns_topic.notify.arn
  protocol  = "email"
  endpoint  = each.value
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
  name               = "aphrodite-aml-export-exec"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "exec" {
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
    # Clio rotates its OAuth refresh token on refresh; the connector persists the
    # rotated value so it survives cold starts. Write scoped to the Clio secret only.
    sid       = "WriteClioToken"
    actions   = ["secretsmanager:PutSecretValue"]
    resources = [var.clio_secret_arn]
  }
  # Decrypt the connector secrets and encrypt the export object (SSE-KMS) with the CMK.
  statement {
    sid       = "UseKms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  statement {
    sid       = "WriteOutputs"
    actions   = ["s3:PutObject"]
    resources = ["${var.outputs_bucket_arn}/aml/*"]
  }
  # Read Aphrodite's include/exclude designation list from the reference bucket.
  statement {
    sid       = "ReadDesignations"
    actions   = ["s3:GetObject"]
    resources = ["${var.reference_bucket_arn}/config/*"]
  }
  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notify.arn]
  }
  # Deployment-config store: a single SSM parameter read-only here (only
  # search_api's admin-gated /config routes write it).
  statement {
    sid       = "ReadDeploymentConfig"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/aphrodite/config/deployment"]
  }
}

resource "aws_iam_role_policy" "exec" {
  name   = "aphrodite-aml-export"
  role   = aws_iam_role.exec.id
  policy = data.aws_iam_policy_document.exec.json
}

# -- lambda -----------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 90
}

resource "aws_lambda_function" "aml_export" {
  function_name    = local.function_name
  role             = aws_iam_role.exec.arn
  runtime          = "python3.13"
  handler          = "backend.lambdas.aml_export.handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  # 900s (max) so the per-contact Clio address lookups for jurisdiction (an N+1
  # under Clio's 50 req/min limit) fit with headroom. Monthly batch — not latency-sensitive.
  timeout     = 900
  memory_size = 1024

  environment {
    variables = {
      APHRODITE_REGION                         = var.aws_region
      APHRODITE_OUTPUTS_BUCKET                 = var.outputs_bucket
      APHRODITE_OUTPUTS_KMS_KEY_ID             = var.kms_key_arn
      APHRODITE_AML_EXPORT_PREFIX              = "aml"
      APHRODITE_NOTIFY_TOPIC_ARN               = aws_sns_topic.notify.arn
      APHRODITE_REFERENCE_BUCKET               = var.reference_bucket
      APHRODITE_AML_DESIGNATIONS_KEY           = "config/designations.xlsx"
      APHRODITE_OWN_COMPANY_NAMES              = jsonencode(var.own_company_names)
      APHRODITE_INTERNAL_TITLE_KEYWORDS        = jsonencode(var.internal_title_keywords)
      APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN = var.internal_matter_number_pattern
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
  name               = "aphrodite-aml-export-scheduler"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-aml-export"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.aml_export.arn
    }]
  })
}

# Fires at midnight on the 1st of each month. Disabled by default until the
# "active" definition and exact column format are confirmed per deployment —
# the Lambda can be invoked manually for the pilot in the meantime.
resource "aws_scheduler_schedule" "aml_export" {
  name       = "aphrodite-aml-export"
  group_name = "default"
  state      = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  target {
    arn      = aws_lambda_function.aml_export.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }

  # The portal can enable/disable and set the monthly cadence at runtime
  # (PUT /aml-exports/schedule). Ignore drift on state + expression so a
  # terraform apply never reverts what staff set in the UI. Timezone stays managed.
  lifecycle {
    ignore_changes = [state, schedule_expression]
  }
}
