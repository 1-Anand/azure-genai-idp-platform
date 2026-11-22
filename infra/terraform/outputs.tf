output "container_app_swagger_url" {
  description = "Live HTTPS OpenAPI Swagger endpoint"
  value       = "https://${azurerm_container_app.app.ingress[0].fqdn}/docs"
}

output "managed_identity_principal_id" {
  description = "System-Assigned Managed Identity Principal ID"
  value       = azurerm_container_app.app.identity[0].principal_id
}
