data "aws_caller_identity" "current" {}

locals {
  function_name = "aphrodite-quarterly-report"
}

# -- lambda package (same build dir as the search Lambda) -------------------

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.root}/build/aphrodite-quarterly-report.zip"
}

# -- staff notification topic ("drafts ready for review") -------------------

resource "aws_sns_topic" "notify" {
  name = "aphrodite-quarterly-report-notify"
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
  name               = "aphrodite-quarterly-report-exec"
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
  # Decrypt the Clio secret and encrypt the report objects (SSE-KMS) with the CMK.
  statement {
    sid       = "UseKms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  statement {
    sid       = "WriteOutputs"
    actions   = ["s3:PutObject"]
    resources = ["${var.outputs_bucket_arn}/quarterly/*"]
  }
  # Read the staff-editable account-classification config sheet (Nicole's Decision A).
  statement {
    sid       = "ReadClassification"
    actions   = ["s3:GetObject"]
    resources = ["${var.reference_bucket_arn}/config/*"]
  }
  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notify.arn]
  }
}

resource "aws_iam_role_policy" "exec" {
  name   = "aphrodite-quarterly-report"
  role   = aws_iam_role.exec.id
  policy = data.aws_iam_policy_document.exec.json
}

# -- lambda -----------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 90
}

resource "aws_lambda_function" "quarterly" {
  function_name    = local.function_name
  role             = aws_iam_role.exec.arn
  runtime          = "python3.13"
  handler          = "backend.lambdas.quarterly_report.handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  # Headroom for a batch run: per-matter Clio reads + the inter-matter pacing delay.
  timeout     = 900
  memory_size = 1024

  environment {
    variables = {
      APHRODITE_REGION                     = var.aws_region
      APHRODITE_OUTPUTS_BUCKET             = var.outputs_bucket
      APHRODITE_OUTPUTS_KMS_KEY_ID         = var.kms_key_arn
      APHRODITE_QUARTERLY_MATTER_IDS       = jsonencode(var.matter_ids)
      APHRODITE_NOTIFY_TOPIC_ARN           = aws_sns_topic.notify.arn
      APHRODITE_REFERENCE_BUCKET           = var.reference_bucket
      APHRODITE_ACCOUNT_CLASSIFICATION_KEY = "config/account_classification.xlsx"
      # Practice-area discovery for scheduled runs (Nicole 2026-08-06): qualifying
      # categories + the "Report Exclusion" checkbox that omits ticked matters.
      APHRODITE_QUARTERLY_QUALIFYING_PRACTICE_AREAS = jsonencode(var.qualifying_practice_areas)
      APHRODITE_QUARTERLY_EXCLUSION_FIELD           = var.exclusion_field
      # Address the letter to Estateably beneficiaries (Nicole 2026-08-06); off ⇒
      # address the Clio client contact (legacy behavior).
      APHRODITE_QUARTERLY_BENEFICIARY_EMAILS = var.beneficiary_emails_enabled ? "true" : "false"
      # SharePoint delivery (scheduled runs only). Off unless a site is configured.
      APHRODITE_SP_UPLOAD_ENABLED              = var.sharepoint_upload_enabled ? "true" : "false"
      APHRODITE_SP_UPLOAD_SITE                 = var.sharepoint_upload_site
      APHRODITE_SP_UPLOAD_FOLDER               = var.sharepoint_upload_folder
      APHRODITE_QUARTERLY_MATTER_DELAY_SECONDS = tostring(var.matter_delay_seconds)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.exec]
}

# -- EventBridge Scheduler (quarterly) --------------------------------------

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
  name               = "aphrodite-quarterly-report-scheduler"
  path               = "/aphrodite/"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-quarterly-report"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.quarterly.arn
    }]
  })
}

# Fires a few weeks after each quarter end (books settled), matching the sample's
# mid-quarter-after send. Disabled by default until HT confirms the matter set /
# calendar — the Lambda can be invoked manually for the pilot in the meantime.
resource "aws_scheduler_schedule" "quarterly" {
  name       = "aphrodite-quarterly-report"
  group_name = "default"
  state      = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  target {
    arn      = aws_lambda_function.quarterly.arn
    role_arn = aws_iam_role.scheduler.arn
    # Previous complete quarter. "discover" builds the matter set from Clio by
    # practice area (minus Report Exclusion); "sharepoint" opts scheduled runs into
    # SharePoint delivery (on-demand portal runs do neither).
    input = jsonencode({ sharepoint = true, discover = true })
  }
}
