variable "secret_arns" {
  type        = list(string)
  description = "Secrets Manager secret ARNs the connectors may read."
}

variable "kms_key_arn" {
  type        = string
  description = "CMK ARN the connectors may use to decrypt secrets."
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Shared execution role for the read-only connector Lambdas. Path must be
# /aphrodite/ so the GitHub Actions deploy role (scoped to that path) can
# manage it. Per-function roles can be split out later as functions are added.
resource "aws_iam_role" "connector" {
  name               = "connector-exec"
  path               = "/aphrodite/"
  description        = "Execution role for read-only source connectors."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "connector" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:*:log-group:/aws/lambda/aphrodite-*"]
  }
  statement {
    sid       = "ReadSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = var.secret_arns
  }
  statement {
    sid       = "DecryptSecrets"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "connector" {
  name   = "connector-access"
  role   = aws_iam_role.connector.id
  policy = data.aws_iam_policy_document.connector.json
}
