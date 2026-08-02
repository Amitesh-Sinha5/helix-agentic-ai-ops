variable "name_prefix" {
  description = "Prefix for every resource name. Must be globally unique enough for ACR and Redis."
  type        = string
  default     = "helix"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name_prefix))
    error_message = "name_prefix must be 3-21 lowercase alphanumerics or hyphens, starting with a letter."
  }
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "westeurope"
}

variable "environment" {
  description = "Environment tag."
  type        = string
  default     = "production"
}

variable "image_tag" {
  description = "Container image tag to deploy (usually the git SHA)."
  type        = string
  default     = "latest"
}

# --- Database ---------------------------------------------------------------
variable "postgres_admin" {
  description = "Postgres administrator login."
  type        = string
  default     = "helixadmin"
}

variable "postgres_password" {
  description = "Postgres administrator password. Pass via TF_VAR_postgres_password or a secret store — never commit it."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.postgres_password) >= 12
    error_message = "postgres_password must be at least 12 characters."
  }
}

variable "postgres_sku" {
  description = "Postgres flexible server SKU."
  type        = string
  default     = "B_Standard_B1ms"
}

# --- Application secrets ----------------------------------------------------
variable "jwt_secret" {
  description = "JWT signing key. Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.jwt_secret) >= 32
    error_message = "jwt_secret must be at least 32 characters."
  }
}

variable "anthropic_api_key" {
  description = "Anthropic API key. Leave empty to run the mock provider."
  type        = string
  sensitive   = true
  default     = ""
}

variable "llm_provider" {
  description = "mock | openai | anthropic"
  type        = string
  default     = "anthropic"

  validation {
    condition     = contains(["mock", "openai", "anthropic"], var.llm_provider)
    error_message = "llm_provider must be one of: mock, openai, anthropic."
  }
}

# --- Networking -------------------------------------------------------------
variable "frontend_origin" {
  description = "Origin allowed by the backend's CORS policy."
  type        = string
  default     = "https://helix.example.com"
}
