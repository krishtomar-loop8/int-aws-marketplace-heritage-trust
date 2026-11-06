data "aws_caller_identity" "current" {}

locals {
  # Entra SAML is wired only once its federation metadata URL is supplied. Until
  # then the app client stays COGNITO-only so the pipeline applies cleanly.
  saml_enabled                 = var.saml_metadata_url != ""
  supported_identity_providers = local.saml_enabled ? ["COGNITO", "EntraID"] : ["COGNITO"]
}

# User pool. Staff federate via Entra ID (SAML); a COGNITO native user is kept as
# admin-create-only break-glass. MFA (TOTP) is enforced for native users; for
# federated users MFA is enforced at Entra via Conditional Access (Cognito MFA
# does not apply to SAML-federated identities).
resource "aws_cognito_user_pool" "main" {
  name = "aphrodite-poc"

  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "aphrodite-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# Brand the (classic) Hosted UI login page: the Aphrodite palette (cream
# background, forest submit button, brass input focus, Spectral serif).
# Applies to the native login form now and the IdP-selection page once SAML is
# switched on. NOTE: this styles the *classic* hosted UI; if AWS provisions
# "Managed Login v2" for the domain, pin managed_login_version = 1.
resource "aws_cognito_user_pool_ui_customization" "web" {
  user_pool_id = aws_cognito_user_pool.main.id
  client_id    = aws_cognito_user_pool_client.web.id

  # No logo image_file — the SPA brands itself with a text wordmark. Add a
  # `filebase64("${path.module}/assets/logo.png")` here to brand the Cognito
  # Hosted UI with a customer logo.

  # Cognito validates each rule against its fixed allow-list of *single* classes —
  # comma-combined selectors are rejected, so every rule targets exactly one class.
  css = <<-CSS
    .background-customizable { background-color: #f7f3ea; }
    .banner-customizable { padding: 28px 0 12px; background-color: #f7f3ea; }
    .logo-customizable { max-width: 260px; max-height: 110px; }
    .textDescription-customizable { color: #17211f; }
    .idpDescription-customizable { color: #17211f; }
    .label-customizable { color: #0d2b26; font-weight: 600; }
    .inputField-customizable {
      border: 1px solid #ddd6c6; border-radius: 4px; padding: 10px 12px;
    }
    .inputField-customizable:focus { border-color: #966410; outline: none; }
    .submitButton-customizable {
      background-color: #123c34; border-radius: 4px; font-weight: 600;
    }
    .submitButton-customizable:hover { background-color: #0d2b26; }
    .idpButton-customizable {
      background-color: #123c34; border-radius: 4px; font-weight: 600;
    }
    .idpButton-customizable:hover { background-color: #0d2b26; }
    .errorMessage-customizable {
      background: #f4e6e1; border: 1px solid #8a2b26; color: #8a2b26;
    }
    .legalText-customizable { color: #6f8880; }
    .redirect-customizable { background-color: #f7f3ea; }
  CSS

  depends_on = [aws_cognito_user_pool_domain.main]
}

# SAML federation to Aphrodite's Entra ID. Created only when the Entra
# enterprise app's federation metadata URL is supplied (see var.saml_metadata_url);
# Cognito re-fetches the URL, so Entra signing-cert rotation is tracked
# automatically. Attribute mapping uses Entra's default claim URIs.
resource "aws_cognito_identity_provider" "entra" {
  count = local.saml_enabled ? 1 : 0

  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = "EntraID"
  provider_type = "SAML"

  provider_details = {
    MetadataURL = var.saml_metadata_url
    IDPSignout  = "false"
  }

  attribute_mapping = {
    email       = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    given_name  = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"
    family_name = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname"
  }
}

# Public client (SPA, authorization-code + PKCE). Callback URLs are placeholders
# until the frontend domain exists (Phase 2) and are updated then.
resource "aws_cognito_user_group" "admins" {
  name         = "admins"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Staff who may connect/reconnect source-system credentials (Setup screen)."
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "aphrodite-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = false

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  supported_identity_providers         = local.supported_identity_providers

  # Ensure the SAML IdP exists before the client references "EntraID".
  depends_on = [aws_cognito_identity_provider.entra]
}
