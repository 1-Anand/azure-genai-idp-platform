# Azure GenAI Intelligent Document Processing (IDP) Platform

![Azure](https://img.shields.io/badge/Azure-Container_Apps_%7C_AI_Services-0078D4?logo=microsoftazure)
![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC?logo=terraform)
![Python](https://img.shields.io/badge/Python-3.12_%7C_FastAPI-3776AB?logo=python)
![Auth](https://img.shields.io/badge/Auth-Zero--Key_Managed_Identity-2ea44f)

A serverless **Intelligent Document Processing (IDP) & GenAI Auditing** microservice built on **Azure AI Document Intelligence**, **Azure OpenAI (`gpt-5-mini`)**, and **Azure Container Apps**, architected for **zero stored API keys** and **scale-to-zero FinOps efficiency**.

## Architecture

```mermaid
flowchart LR
    Client[Client / OpenAPI UI] -->|HTTPS POST PDF| ACA[Azure Container App<br/>FastAPI Non-Root Container<br/>Scale-to-Zero]
    ACA -->|System-Assigned Managed Identity<br/>Cognitive Services User| DocInt[Azure AI Document Intelligence<br/>prebuilt-invoice]
    DocInt -->|Structured Fields + Confidence| ACA
    ACA -->|Entra ID Bearer Token<br/>Cognitive Services OpenAI User| AOAI[Azure OpenAI<br/>gpt-5-mini]
    AOAI -->|Normalized JSON + Audit Flag| ACA
    ACA -->|HTTP 200 ERP-Ready JSON| Client
```

## Core Platform Capabilities

- **Zero-Key Security:** Authenticates end-to-end with `DefaultAzureCredential` and least-privilege Data-Plane RBAC — zero API keys in container images or runtime environment variables.
- **Confidence-Gated Auditing:** Normalizes dates (YYYY-MM-DD) and numeric amounts, verifies `SubTotal + TotalTax == InvoiceTotal`, and flags low-confidence extractions (< 0.85) for manual review.
- **Infrastructure as Code:** Modular Terraform (`infra/terraform/`) covering AI resources, model deployment, Container Apps environment, and RBAC role assignments.

See [docs/architecture-decisions.md](docs/architecture-decisions.md) for detailed design rationales.
