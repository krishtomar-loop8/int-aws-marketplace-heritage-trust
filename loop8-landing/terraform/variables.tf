variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = <<-EOT
    AWS region for the landing stack (Loop8's own account — unrelated to the
    customer-deployed product's data-residency region choice, since this
    stack holds no customer operational data). Defaults to us-east-1 so the
    CloudFront ACM certificate (which must be us-east-1) can also serve the
    API Gateway custom domain's regional certificate, avoiding a second ACM
    cert + DNS validation round trip. Overriding this mints a second,
    region-local cert for the API domain instead (see api.tf).
  EOT
}

variable "hosted_zone_name" {
  type        = string
  default     = "loop8.io"
  description = "Existing Route 53 public hosted zone, already controlled by Loop8 in this account."
}

variable "site_domain" {
  type        = string
  default     = "deploy.loop8.io"
  description = "Custom domain for the static landing-page site (CloudFront)."
}

variable "api_domain" {
  type        = string
  default     = "api.deploy.loop8.io"
  description = "Custom domain for the fulfillment/subscribe HTTP API."
}
