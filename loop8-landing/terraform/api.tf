# -- fulfillment/subscribe Lambda -------------------------------------------
# Task 3 writes the real handler (loop8-landing/backend/handler.py); this
# placeholder package lets `terraform apply` create the function before that
# code exists — the same "infra declared ahead of the code that fills it"
# split terraform/modules/search_api/main.tf uses for its own Lambda zip.
# Once Task 3 lands, point `source_dir` below at the real backend directory
# (or have Task 5's CI build/upload the real package over this one).
data "archive_file" "lambda_placeholder" {
  type        = "zip"
  source_dir  = "${path.module}/lambda_placeholder"
  output_path = "${path.module}/build/loop8-landing-api.zip"
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/loop8-landing-api"
  retention_in_days = 90
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

# Path scoped to /loop8-landing/ to match the bootstrap deploy role's IAM
# grant (loop8-landing/bootstrap/bootstrap.yaml's `ManageProjectScopedIamResources`
# statement, resource arn:aws:iam::<account>:role/loop8-landing/*).
resource "aws_iam_role" "api" {
  name               = "loop8-landing-api-exec"
  path               = "/loop8-landing/"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
  # Pending-resolution (SESSION#) and subscriber (SUB#) records — see dynamodb.tf.
  statement {
    sid       = "Subscriptions"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.subscriptions.arn]
  }
  # ResolveCustomer has no resource-level permissions in AWS's IAM reference —
  # the action itself is the only thing to scope; AWS always evaluates it
  # against Resource "*", so a narrower resource here would just be inert.
  statement {
    sid       = "ResolveMarketplaceCustomer"
    actions   = ["aws-marketplace:ResolveCustomer"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "loop8-landing-api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_lambda_function" "api" {
  function_name    = "loop8-landing-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.13"
  handler          = "handler.handler"
  filename         = data.archive_file.lambda_placeholder.output_path
  source_code_hash = data.archive_file.lambda_placeholder.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      SUBSCRIPTIONS_TABLE    = aws_dynamodb_table.subscriptions.name
      LAUNCH_REGION          = var.launch_region
      LAUNCH_STACK_NAME      = var.launch_stack_name
      INSTALLER_TEMPLATE_URL = "https://${aws_s3_bucket.installer_mirror.bucket_regional_domain_name}/installer/latest/installer.yaml"
    }
  }

  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.api]
}

# -- HTTP API Gateway (matches terraform/modules/search_api/main.tf's choice) -

resource "aws_apigatewayv2_api" "api" {
  name          = "loop8-landing-api"
  protocol_type = "HTTP"

  # The frontend (deploy.loop8.io) calls this API cross-origin
  # (api.deploy.loop8.io is a different subdomain), so the browser's preflight
  # OPTIONS request needs an answer — API Gateway handles OPTIONS
  # automatically from this block, same mechanism
  # terraform/modules/search_api/main.tf's HTTP API uses for its own
  # cross-origin SPA calls.
  cors_configuration {
    allow_origins = ["https://${var.site_domain}"]
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["content-type"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

# No Cognito/JWT authorizer here — this flow's only security boundary is the
# Marketplace token's own server-side ResolveCustomer check (per the design
# doc's explicit anti-goal against a new admin-auth system; there is no buyer
# identity yet at this point in the flow).
resource "aws_apigatewayv2_route" "fulfillment" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /fulfillment"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "subscribe" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /subscribe"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

# -- custom domain for the API ----------------------------------------------
# HTTP API custom domains are REGIONAL only (no edge-optimized option), so
# the certificate must live in the API's own region (var.aws_region) — not
# necessarily us-east-1. A certificate only covers the exact hostname(s) it
# was issued for, so this domain (api.deploy.loop8.io) needs its own
# certificate regardless of region — it cannot reuse site.tf's cert, which
# was issued only for deploy.loop8.io. Always provision a dedicated cert here
# (simpler and more robust than conditionally sharing one cert across two
# different hostnames via SANs); ACM certificates are free, so there's no
# cost reason to share one.

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "api_validation" {
  for_each = {
    for dvo in aws_acm_certificate.api.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.loop8.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for r in aws_route53_record.api_validation : r.fqdn]
}

resource "aws_apigatewayv2_domain_name" "api" {
  domain_name = var.api_domain

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.api.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "api" {
  api_id      = aws_apigatewayv2_api.api.id
  domain_name = aws_apigatewayv2_domain_name.api.id
  stage       = aws_apigatewayv2_stage.default.id
}

resource "aws_route53_record" "api" {
  zone_id = data.aws_route53_zone.loop8.zone_id
  name    = var.api_domain
  type    = "A"

  alias {
    name                   = aws_apigatewayv2_domain_name.api.domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.api.domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}

# -- installer-mirror bucket -------------------------------------------------
# Task 5's release workflow copies installer.yaml here after each release, at
# installer/<version>/installer.yaml and installer/latest/installer.yaml (the
# key the landing Lambda's launch-URL templateURL points at). CloudFormation's
# quick-create console fetches templateURL anonymously over plain HTTPS, so
# only that one prefix is public — the rest of the bucket stays private.

resource "aws_s3_bucket" "installer_mirror" {
  bucket = "loop8-landing-installer-mirror-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "installer_mirror" {
  bucket = aws_s3_bucket.installer_mirror.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "installer_mirror" {
  bucket = aws_s3_bucket.installer_mirror.id
  # ACLs stay blocked (no ACL-based public access anywhere) — the only public
  # access is the narrowly-scoped bucket policy below, which requires these
  # two to be false or S3 would block it wholesale.
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

data "aws_iam_policy_document" "installer_mirror" {
  statement {
    sid       = "PublicReadInstallerTemplate"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.installer_mirror.arn}/installer/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "installer_mirror" {
  bucket = aws_s3_bucket.installer_mirror.id
  policy = data.aws_iam_policy_document.installer_mirror.json
}
