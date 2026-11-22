# Architectural Decision Records (ADR)

## ADR-001: Hybrid Document Intelligence + LLM Validation Pipeline
- **Context:** Raw OCR extracts text without semantic structure, while sending raw documents directly to an LLM increases token cost and lacks deterministic per-field confidence scores.
- **Decision:** Use **Azure AI Document Intelligence (`prebuilt-invoice`)** for deterministic layout/table extraction and confidence scoring, followed by **Azure OpenAI (`gpt-5-mini`)** strictly as a downstream schema normalizer and arithmetic auditor.
- **Outcome:** Deterministic confidence gating (`< 0.85` triggers human-in-the-loop review) combined with clean ISO date/currency normalization.

## ADR-002: Zero-Key Passwordless Authentication via Entra ID RBAC
- **Context:** Static Cognitive Services API keys create secret-rotation overhead and credential-leakage risk in containerized environments.
- **Decision:** Enable **System-Assigned Managed Identity** on Azure Container Apps and authenticate via `DefaultAzureCredential` over custom subdomain endpoints.
- **RBAC Scope:** Assign least-privilege Data-Plane roles (`Cognitive Services User` on Document Intelligence; `Cognitive Services OpenAI User` on Azure OpenAI) rather than control-plane-only `Contributor` roles.

## ADR-003: Model Lifecycle & Soft-Delete Governance
- **Context:** Azure AI resources enforce a 48-hour soft-delete retention policy that blocks quota reuse during ephemeral environment rebuilds, and older OpenAI model versions transition to `ServiceModelDeprecating`.
- **Decision:** Parameterize `openai_model_version` in Terraform (`2025-08-07` for `gpt-5-mini`) and enable `purge_soft_delete_on_destroy = true` in the `azurerm` provider block for non-production environments.
