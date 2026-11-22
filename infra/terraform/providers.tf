terraform {
  required_version = ">= 1.6.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {
    cognitive_account {
      # Automatically purges soft-deleted Cognitive Services accounts in non-prod teardown
      purge_soft_delete_on_destroy = true
    }
  }
}
