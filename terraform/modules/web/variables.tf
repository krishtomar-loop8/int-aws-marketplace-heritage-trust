variable "custom_domain" {
  type        = string
  default     = ""
  description = "Custom domain for the SPA (e.g. app.aphrodite.example). Empty = CloudFront default domain only."
}

variable "enable_custom_domain" {
  type        = bool
  default     = false
  description = "When true, attach the (already-issued) ACM cert + domain alias to CloudFront. See root var of the same name."
}
