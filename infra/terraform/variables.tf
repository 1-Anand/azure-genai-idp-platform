variable "location" {
  description = "Azure region for all AI and serverless compute resources"
  type        = string
  default     = "eastus"
}

variable "resource_group_name" {
  description = "Target Azure Resource Group name"
  type        = string
  default     = "rg-genai-idp-dev"
}

variable "docker_image" {
  description = "Container image reference (e.g., <registry>/invoice-engine:v1)"
  type        = string
}

variable "openai_model_version" {
  description = "Active GA model version for Azure OpenAI deployment"
  type        = string
  default     = "2025-08-07"
}

variable "idp_api_key" {
  description = "Optional custom API key for /analyze-invoice data-plane calls. If omitted, a secure 32-character key is automatically generated."
  type        = string
  sensitive   = true
  default     = ""
}
