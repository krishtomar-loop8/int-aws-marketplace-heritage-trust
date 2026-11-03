variable "aws_region" {
  type        = string
  description = "AWS region (used to build the hosted-UI / SAML ACS URL)."
}

variable "saml_metadata_url" {
  type        = string
  default     = ""
  description = <<-EOT
    Entra ID "App Federation Metadata URL" for the SAML enterprise app. Empty
    until the Entra app is created; when set, the EntraID SAML IdP is created and
    added to the app client. Not secret (contains the tenant id only).
  EOT
}

variable "callback_urls" {
  type        = list(string)
  default     = ["https://localhost:3000/callback"]
  description = "OAuth callback URLs for the web app client (localhost + CloudFront)."
}

variable "logout_urls" {
  type        = list(string)
  default     = ["https://localhost:3000/logout"]
  description = "OAuth logout URLs for the web app client."
}
