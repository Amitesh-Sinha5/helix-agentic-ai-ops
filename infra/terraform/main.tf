/**
 * Helix on Azure — minimal, opinionated stub.
 *
 * Provisions the four things the app actually needs:
 *   - Azure Database for PostgreSQL (flexible server)
 *   - Azure Cache for Redis
 *   - Azure Container Registry
 *   - Azure Container Apps for the backend and frontend
 *
 * This is a starting point, not a hardened production module. What it does get
 * right: no secrets in state as plaintext defaults, private-ish networking
 * defaults, and the backend scaled on HTTP concurrency rather than CPU (agent
 * requests are I/O-bound on the model, so CPU is a poor scaling signal).
 *
 *   terraform init
 *   terraform plan  -var="postgres_password=..." -var="jwt_secret=..."
 *   terraform apply -var="postgres_password=..." -var="jwt_secret=..."
 */

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
  # Uncomment once a state storage account exists. Local state is fine for a
  # demo and wrong for anything shared.
  # backend "azurerm" {
  #   resource_group_name  = "helix-tfstate"
  #   storage_account_name = "helixtfstate"
  #   container_name       = "tfstate"
  #   key                  = "helix.tfstate"
  # }
}

provider "azurerm" {
  features {}
}

# --------------------------------------------------------------------------
# Foundation
# --------------------------------------------------------------------------
resource "azurerm_resource_group" "helix" {
  name     = "${var.name_prefix}-rg"
  location = var.location
  tags     = local.tags
}

resource "azurerm_container_registry" "helix" {
  name                = replace("${var.name_prefix}acr", "-", "")
  resource_group_name = azurerm_resource_group.helix.name
  location            = azurerm_resource_group.helix.location
  sku                 = "Basic"
  admin_enabled       = true
  tags                = local.tags
}

resource "azurerm_log_analytics_workspace" "helix" {
  name                = "${var.name_prefix}-logs"
  resource_group_name = azurerm_resource_group.helix.name
  location            = azurerm_resource_group.helix.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

# --------------------------------------------------------------------------
# Data stores
# --------------------------------------------------------------------------
resource "azurerm_postgresql_flexible_server" "helix" {
  name                          = "${var.name_prefix}-pg"
  resource_group_name           = azurerm_resource_group.helix.name
  location                      = azurerm_resource_group.helix.location
  version                       = "16"
  administrator_login           = var.postgres_admin
  administrator_password        = var.postgres_password
  sku_name                      = var.postgres_sku
  storage_mb                    = 32768
  backup_retention_days         = 7
  public_network_access_enabled = true # tighten to a private endpoint for production
  zone                          = "1"
  tags                          = local.tags

  lifecycle {
    # Azure resizes storage in place and reports drift on every plan.
    ignore_changes = [storage_mb, zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "helix" {
  name      = "helix"
  server_id = azurerm_postgresql_flexible_server.helix.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Container Apps egress IPs are not static, so this rule allows Azure services
# through. Replace with VNet integration + a private endpoint for production.
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure_services" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.helix.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_redis_cache" "helix" {
  name                          = "${var.name_prefix}-redis"
  resource_group_name           = azurerm_resource_group.helix.name
  location                      = azurerm_resource_group.helix.location
  capacity                      = 0
  family                        = "C"
  sku_name                      = "Basic"
  non_ssl_port_enabled          = false
  minimum_tls_version           = "1.2"
  public_network_access_enabled = true
  tags                          = local.tags

  redis_configuration {
    # Helix's Redis holds the semantic cache, rate-limit windows, session memory
    # and the trace replay buffer — all reconstructible, all TTL'd. Evicting the
    # least-recently-used key under pressure is correct; refusing writes is not.
    maxmemory_policy = "allkeys-lru"
  }
}

# --------------------------------------------------------------------------
# Compute
# --------------------------------------------------------------------------
resource "azurerm_container_app_environment" "helix" {
  name                       = "${var.name_prefix}-env"
  resource_group_name        = azurerm_resource_group.helix.name
  location                   = azurerm_resource_group.helix.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.helix.id
  tags                       = local.tags
}

resource "azurerm_container_app" "backend" {
  name                         = "${var.name_prefix}-backend"
  resource_group_name          = azurerm_resource_group.helix.name
  container_app_environment_id = azurerm_container_app_environment.helix.id
  revision_mode                = "Single"
  tags                         = local.tags

  registry {
    server               = azurerm_container_registry.helix.login_server
    username             = azurerm_container_registry.helix.admin_username
    password_secret_name = "acr-password"
  }

  secret {
    name  = "acr-password"
    value = azurerm_container_registry.helix.admin_password
  }
  secret {
    name  = "database-url"
    value = "postgresql+asyncpg://${var.postgres_admin}:${var.postgres_password}@${azurerm_postgresql_flexible_server.helix.fqdn}:5432/helix"
  }
  secret {
    name  = "redis-url"
    value = "rediss://:${azurerm_redis_cache.helix.primary_access_key}@${azurerm_redis_cache.helix.hostname}:6380/0"
  }
  secret {
    name  = "jwt-secret"
    value = var.jwt_secret
  }
  secret {
    name  = "anthropic-api-key"
    value = var.anthropic_api_key
  }

  template {
    min_replicas = 1
    max_replicas = 10

    container {
      name   = "backend"
      image  = "${azurerm_container_registry.helix.login_server}/helix-backend:${var.image_tag}"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "REDIS_URL"
        secret_name = "redis-url"
      }
      env {
        name        = "JWT_SECRET_KEY"
        secret_name = "jwt-secret"
      }
      env {
        name        = "ANTHROPIC_API_KEY"
        secret_name = "anthropic-api-key"
      }
      env {
        name  = "ENVIRONMENT"
        value = "production"
      }
      env {
        name  = "LLM_PROVIDER"
        value = var.llm_provider
      }
      env {
        name  = "REDIS_REQUIRED"
        value = "true"
      }
      env {
        name  = "CORS_ORIGINS"
        value = var.frontend_origin
      }

      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 8000
      }
      readiness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 8000
      }
    }

    # Agent requests are I/O-bound waiting on the model, so CPU stays low even
    # when the app is saturated. Concurrency is the signal that matters.
    http_scale_rule {
      name                = "http-concurrency"
      concurrent_requests = 20
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto" # negotiates HTTP/2 and passes WebSockets through

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

resource "azurerm_container_app" "frontend" {
  name                         = "${var.name_prefix}-frontend"
  resource_group_name          = azurerm_resource_group.helix.name
  container_app_environment_id = azurerm_container_app_environment.helix.id
  revision_mode                = "Single"
  tags                         = local.tags

  registry {
    server               = azurerm_container_registry.helix.login_server
    username             = azurerm_container_registry.helix.admin_username
    password_secret_name = "acr-password"
  }

  secret {
    name  = "acr-password"
    value = azurerm_container_registry.helix.admin_password
  }

  template {
    min_replicas = 1
    max_replicas = 3

    container {
      name   = "frontend"
      image  = "${azurerm_container_registry.helix.login_server}/helix-frontend:${var.image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

locals {
  tags = {
    application = "helix"
    managed_by  = "terraform"
    environment = var.environment
  }
}
