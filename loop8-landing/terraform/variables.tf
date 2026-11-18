variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = <<-EOT
    AWS region for the landing stack (Loop8's own account — unrelated to the
    customer-deployed product's data-residency region choice, since this
    stack holds no customer operational data). CloudFront's ACM certificate
    (site.tf) always lives in us-east-1 regardless of this value; the API
    Gateway custom domain's certificate (api.tf) always lives in this
    region — the two are separate certificates for separate hostnames
    either way, so this is a free choice of where the rest of the stack
    (API Gateway, Lambda, DynamoDB) runs.
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

variable "launch_region" {
  type        = string
  default     = "ca-central-1"
  description = "Region the buyer's CloudFormation launch-link defaults to (the customer-deployed product's region, unrelated to var.aws_region above)."
}

variable "launch_stack_name" {
  type        = string
  default     = "Aphrodite"
  description = "Default CloudFormation stack name pre-filled in the buyer's launch link."
}

variable "expected_product_code" {
  type        = string
  default     = ""
  description = "ResolveCustomer's ProductCode must match this value, if set (handler.py's pinning check). Left empty (the default) disables the check - set it once Loop8's AWS Marketplace listing's product code is known."
}
