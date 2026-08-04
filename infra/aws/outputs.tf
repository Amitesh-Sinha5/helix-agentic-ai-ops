output "api_url" {
  description = "Public URL of the Helix API."
  value       = var.acm_certificate_arn == "" ? "http://${aws_lb.main.dns_name}" : "https://${aws_lb.main.dns_name}"
}

output "frontend_url" {
  description = "Public URL of the web app."
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "ecr_repository" {
  description = "Push the backend image here."
  value       = aws_ecr_repository.backend.repository_url
}

output "frontend_bucket" {
  description = "Sync the built frontend here."
  value       = aws_s3_bucket.frontend.id
}

output "stripe_webhook_url" {
  description = "Register this in the Stripe dashboard."
  value       = "${var.acm_certificate_arn == "" ? "http://" : "https://"}${aws_lb.main.dns_name}/billing/webhook"
}

output "deploy_commands" {
  description = "Copy-paste deployment steps."
  value       = <<-EOT
    # 1. Build and push the backend
    aws ecr get-login-password --region ${var.region} \
      | docker login --username AWS --password-stdin ${aws_ecr_repository.backend.repository_url}
    docker build -t ${aws_ecr_repository.backend.repository_url}:${var.image_tag} ./backend
    docker push ${aws_ecr_repository.backend.repository_url}:${var.image_tag}

    # 2. Run migrations ONCE as a standalone task (never from the entrypoint,
    #    or replicas race each other on the same database)
    aws ecs run-task --cluster ${aws_ecs_cluster.main.name} \
      --task-definition ${aws_ecs_task_definition.backend.family} \
      --launch-type FARGATE --region ${var.region} \
      --network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.public[*].id)}],securityGroups=[${aws_security_group.app.id}],assignPublicIp=ENABLED}' \
      --overrides '{"containerOverrides":[{"name":"backend","command":["alembic","upgrade","head"]}]}'

    # 3. Roll the service
    aws ecs update-service --cluster ${aws_ecs_cluster.main.name} \
      --service ${aws_ecs_service.backend.name} --force-new-deployment --region ${var.region}

    # 4. Build and publish the frontend (the API URL is baked in at build time)
    cd frontend && VITE_API_URL=http://${aws_lb.main.dns_name} npm run build
    aws s3 sync dist/ s3://${aws_s3_bucket.frontend.id}/ --delete
    aws cloudfront create-invalidation \
      --distribution-id ${aws_cloudfront_distribution.frontend.id} --paths '/*'

    # 5. Point CORS at the real frontend and re-apply
    terraform apply -var="frontend_origin=https://${aws_cloudfront_distribution.frontend.domain_name}"

    # 6. Sign up at the frontend — the FIRST account becomes the administrator.
  EOT
}
