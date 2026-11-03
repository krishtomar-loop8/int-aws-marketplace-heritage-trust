output "user_pool_id" {
  description = "Cognito user pool ID."
  value       = aws_cognito_user_pool.main.id
}

output "user_pool_client_id" {
  description = "Cognito app client ID (web SPA)."
  value       = aws_cognito_user_pool_client.web.id
}

output "hosted_ui_domain" {
  description = "Cognito hosted-UI domain prefix."
  value       = aws_cognito_user_pool_domain.main.domain
}

# SP coordinates to register in the Entra ID SAML enterprise application.
output "saml_sp_entity_id" {
  description = "SAML SP Entity ID (Identifier) to set in the Entra enterprise app."
  value       = "urn:amazon:cognito:sp:${aws_cognito_user_pool.main.id}"
}

output "saml_acs_url" {
  description = "SAML ACS / Reply URL to set in the Entra enterprise app."
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com/saml2/idpresponse"
}
