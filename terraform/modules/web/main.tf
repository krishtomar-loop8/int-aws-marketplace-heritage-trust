data "aws_caller_identity" "current" {}

# Private S3 origin for the SPA (served only via CloudFront OAC — never public).
resource "aws_s3_bucket" "web" {
  bucket = "aphrodite-web-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "web" {
  bucket = aws_s3_bucket.web.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "aphrodite-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ACM certificate for the custom domain — must live in us-east-1 for CloudFront.
# Created as soon as `custom_domain` is set so the DNS validation records can be
# handed to HT; attached to CloudFront only once `enable_custom_domain` is true
# (see the two-phase toggle in variables.tf).
resource "aws_acm_certificate" "web" {
  count             = var.custom_domain == "" ? 0 : 1
  provider          = aws.us_east_1
  domain_name       = var.custom_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# HT manages DNS externally, so we don't create the validation record — this
# resource simply waits until the cert reaches ISSUED (i.e. until HT has added
# the CNAME from the `acm_validation_records` output). Only created on Apply 2.
resource "aws_acm_certificate_validation" "web" {
  count           = var.enable_custom_domain ? 1 : 0
  provider        = aws.us_east_1
  certificate_arn = aws_acm_certificate.web[0].arn
}

resource "aws_cloudfront_distribution" "web" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "Aphrodite — matter search SPA"
  price_class         = "PriceClass_100"

  # Attached only on Apply 2, once the cert is issued (see viewer_certificate).
  aliases = var.enable_custom_domain ? [var.custom_domain] : null

  origin {
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_id                = "s3-web"
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-web"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # AWS managed "CachingOptimized" policy.
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # SPA fallback: unknown paths return index.html so client-side routing works.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Default CloudFront cert until the custom domain is enabled; then the issued
  # ACM cert (referenced via the validation resource so CloudFront only picks it
  # up once the cert is ISSUED).
  viewer_certificate {
    cloudfront_default_certificate = var.enable_custom_domain ? null : true
    acm_certificate_arn            = var.enable_custom_domain ? aws_acm_certificate_validation.web[0].certificate_arn : null
    ssl_support_method             = var.enable_custom_domain ? "sni-only" : null
    minimum_protocol_version       = var.enable_custom_domain ? "TLSv1.2_2021" : null
  }
}

# Allow only this CloudFront distribution to read the bucket.
data "aws_iam_policy_document" "web" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.web.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.web.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = data.aws_iam_policy_document.web.json
}
