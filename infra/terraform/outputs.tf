output "backend_url" {
  description = "Public URL of the Helix API."
  value       = "https://${azurerm_container_app.backend.ingress[0].fqdn}"
}

output "frontend_url" {
  description = "Public URL of the Helix web app."
  value       = "https://${azurerm_container_app.frontend.ingress[0].fqdn}"
}

output "container_registry" {
  description = "Registry to push images to before deploying."
  value       = azurerm_container_registry.helix.login_server
}

output "postgres_fqdn" {
  description = "Postgres host."
  value       = azurerm_postgresql_flexible_server.helix.fqdn
}

output "redis_hostname" {
  description = "Redis host."
  value       = azurerm_redis_cache.helix.hostname
}

output "stripe_webhook_url" {
  description = "Register this URL in the Stripe dashboard."
  value       = "https://${azurerm_container_app.backend.ingress[0].fqdn}/billing/webhook"
}

output "next_steps" {
  description = "What to do after apply."
  value       = <<-EOT
    1. Build and push images:
         az acr login --name ${azurerm_container_registry.helix.name}
         docker build -t ${azurerm_container_registry.helix.login_server}/helix-backend:${var.image_tag} ./backend
         docker push ${azurerm_container_registry.helix.login_server}/helix-backend:${var.image_tag}
         docker build --build-arg VITE_API_URL=https://${azurerm_container_app.backend.ingress[0].fqdn} \
           -t ${azurerm_container_registry.helix.login_server}/helix-frontend:${var.image_tag} ./frontend
         docker push ${azurerm_container_registry.helix.login_server}/helix-frontend:${var.image_tag}

    2. Migrations run from the backend entrypoint on first boot
       (set RUN_MIGRATIONS=false and run them as a job once you scale past one replica).

    3. Register the Stripe webhook at:
         https://${azurerm_container_app.backend.ingress[0].fqdn}/billing/webhook

    4. The first account created at /signup becomes the administrator.
  EOT
}
