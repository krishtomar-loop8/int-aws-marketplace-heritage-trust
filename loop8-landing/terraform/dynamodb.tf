# Single table, two record shapes sharing one partition-key prefix scheme
# (see docs/superpowers/specs/2026-11-17-marketplace-landing-page-design.md):
#   pk = "SESSION#<uuid>" - pending Marketplace-token resolution. Short-lived:
#     `used` (bool) + `expires_at` (TTL, ~30 min out) so AWS auto-deletes
#     unused sessions. Attributes: CustomerAWSAccountId, ProductCode,
#     CustomerIdentifier (the resolved ResolveCustomer identifiers), used,
#     expires_at.
#   pk = "SUB#<uuid>"     - durable subscriber record, no TTL. Attributes:
#     company_name, admin_email, the resolved identifiers, created_at.
# Hash key only - no sort key, no GSI (no query need beyond GetItem by pk yet,
# per the design doc's own "not over-built" call).
resource "aws_dynamodb_table" "subscriptions" {
  name         = "loop8-landing-subscriptions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Lead-tracking data only (company name + business email) - not subject to
  # the deployed product's own data-handling rules, so DynamoDB's default
  # (AWS-owned key) encryption is sufficient; no customer-managed CMK needed.
  server_side_encryption {
    enabled = true
  }
}
