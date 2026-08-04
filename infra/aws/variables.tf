variable "region" {
  description = "AWS region."
  type        = string
  default     = "ap-south-1" # Mumbai — closest to Bengaluru
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "helix"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name_prefix))
    error_message = "name_prefix must be 3-21 lowercase alphanumerics or hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "staging | production. Controls deletion protection, backups and log retention."
  type        = string
  default     = "staging"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "image_tag" {
  description = "Backend image tag to deploy (usually the git SHA)."
  type        = string
  default     = "latest"
}

# --- Compute -----------------------------------------------------------------
variable "task_cpu" {
  description = "Fargate CPU units. 512 = 0.5 vCPU."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate memory (MiB). Must be a valid pair with task_cpu."
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "Initial task count."
  type        = number
  default     = 1
}

variable "min_capacity" {
  type    = number
  default = 1
}

variable "max_capacity" {
  type    = number
  default = 4
}

# --- Data stores -------------------------------------------------------------
variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is free-tier eligible for the first 12 months."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_username" {
  type    = string
  default = "helixadmin"
}

variable "db_password" {
  description = "Postgres password. Pass via TF_VAR_db_password; never commit it."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.db_password) >= 12
    error_message = "db_password must be at least 12 characters."
  }
}

variable "db_multi_az" {
  description = "Roughly doubles RDS cost. Leave false for a demo."
  type        = bool
  default     = false
}

variable "redis_node_type" {
  description = "cache.t4g.micro is free-tier eligible for the first 12 months."
  type        = string
  default     = "cache.t4g.micro"
}

variable "redis_multi_az" {
  description = "Adds a replica. Leave false for a demo."
  type        = bool
  default     = false
}

# --- Application secrets -----------------------------------------------------
variable "jwt_secret" {
  description = "JWT signing key. python -c \"import secrets; print(secrets.token_urlsafe(48))\""
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.jwt_secret) >= 32
    error_message = "jwt_secret must be at least 32 characters."
  }
}

variable "llm_provider" {
  description = "mock | openai | anthropic"
  type        = string
  default     = "mock"

  validation {
    condition     = contains(["mock", "openai", "anthropic"], var.llm_provider)
    error_message = "llm_provider must be mock, openai or anthropic."
  }
}

variable "embedding_provider" {
  type    = string
  default = "mock"
}

variable "anthropic_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "openai_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "stripe_secret_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "stripe_webhook_secret" {
  type      = string
  sensitive = true
  default   = ""
}

# --- Networking --------------------------------------------------------------
variable "frontend_origin" {
  description = "Origin allowed by the backend's CORS policy. Set to the CloudFront URL after the first apply."
  type        = string
  default     = "http://localhost:5173"
}

variable "acm_certificate_arn" {
  description = "ACM certificate in this region for the ALB. Empty means HTTP only (fine for a demo, not for real traffic)."
  type        = string
  default     = ""
}

variable "cloudfront_price_class" {
  description = "PriceClass_100 is cheapest (NA + EU); PriceClass_200 adds Asia."
  type        = string
  default     = "PriceClass_200"
}
