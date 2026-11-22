resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
}

# Azure AI Document Intelligence (Enforces Zero-Key passwordless Entra ID authentication)
resource "azurerm_cognitive_account" "docint" {
  name                  = "docint-idp-dev-01"
  location              = azurerm_resource_group.rg.location
  resource_group_name   = azurerm_resource_group.rg.name
  kind                  = "FormRecognizer"
  sku_name              = "F0"
  custom_subdomain_name = "docint-idp-dev-01"
  local_auth_enabled    = false
}

# Azure OpenAI Service + gpt-5-mini Deployment (Enforces Zero-Key passwordless Entra ID authentication)
resource "azurerm_cognitive_account" "openai" {
  name                  = "aoai-idp-dev-01"
  location              = azurerm_resource_group.rg.location
  resource_group_name   = azurerm_resource_group.rg.name
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "aoai-idp-dev-01"
  local_auth_enabled    = false
}

resource "azurerm_cognitive_deployment" "gpt5_mini" {
  name                 = "gpt-5-mini"
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = "gpt-5-mini"
    version = var.openai_model_version
  }

  sku {
    name     = "GlobalStandard"
    capacity = 10 # 10K Tokens Per Minute (TPM)
  }
}

# Generate secure random API key if not explicitly supplied
resource "random_password" "api_key" {
  length  = 32
  special = false
}

locals {
  effective_api_key = var.idp_api_key != "" ? var.idp_api_key : random_password.api_key.result
}

# Serverless Azure Container Apps (Scale-to-Zero + System-Assigned Managed Identity)
resource "azurerm_container_app_environment" "env" {
  name                = "cae-genai-idp-dev"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_container_app" "app" {
  name                         = "ca-genai-idp-engine"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"

  identity {
    type = "SystemAssigned"
  }

  # Native ACA Secret Reference (Never exposes raw key plaintext in container definitions)
  secret {
    name  = "idp-api-key"
    value = local.effective_api_key
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = 0
    max_replicas = 3

    http_scale_rule {
      name                = "http-concurrency-rule"
      concurrent_requests = "10"
    }

    container {
      name   = "idp-engine"
      image  = var.docker_image
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "DOCINT_ENDPOINT"
        value = azurerm_cognitive_account.docint.endpoint
      }
      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = azurerm_cognitive_account.openai.endpoint
      }
      env {
        name  = "AZURE_OPENAI_DEPLOYMENT"
        value = azurerm_cognitive_deployment.gpt5_mini.name
      }
      env {
        name        = "IDP_API_KEY"
        secret_name = "idp-api-key"
      }
    }
  }
}

# Zero-Key Data-Plane RBAC Assignments
resource "azurerm_role_assignment" "docint_user" {
  scope                = azurerm_cognitive_account.docint.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_container_app.app.identity[0].principal_id
}

resource "azurerm_role_assignment" "openai_user" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_container_app.app.identity[0].principal_id
}
