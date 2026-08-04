/**
 * ECR, ECS Fargate service, and the load balancer in front of it.
 */

resource "aws_ecr_repository" "backend" {
  name                 = "${local.name}-backend"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.environment != "production"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${local.name}-backend"
  retention_in_days = 30
}

# --- IAM ---------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Pulls images, writes logs, reads the secret. Used by the ECS agent.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-app-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.app.arn]
    }]
  })
}

# Used by the application itself, not the agent. Only needs EFS.
resource "aws_iam_role" "task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task_efs" {
  name = "chroma-volume"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
        "elasticfilesystem:ClientRootAccess",
      ]
      Resource = [aws_efs_file_system.chroma.arn]
    }]
  })
}

# --- Task definition ---------------------------------------------------------
locals {
  backend_image = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"

  backend_environment = [
    { name = "ENVIRONMENT", value = var.environment },
    { name = "DEBUG", value = "false" },
    { name = "LLM_PROVIDER", value = var.llm_provider },
    { name = "EMBEDDING_PROVIDER", value = var.embedding_provider },
    { name = "REDIS_REQUIRED", value = "true" },
    { name = "CHROMA_PERSIST_DIR", value = "/data/chroma" },
    { name = "CORS_ORIGINS", value = var.frontend_origin },
    { name = "BILLING_SUCCESS_URL", value = "${var.frontend_origin}/billing?status=success" },
    { name = "BILLING_CANCEL_URL", value = "${var.frontend_origin}/billing?status=cancelled" },
    # Migrations run as a one-off task, never from the entrypoint: several
    # replicas starting at once must not race to migrate the same database.
    { name = "RUN_MIGRATIONS", value = "false" },
  ]

  backend_secrets = [
    for key in [
      "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY",
      "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
      "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    ] : { name = key, valueFrom = "${aws_secretsmanager_secret.app.arn}:${key}::" }
  ]
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = var.environment == "production" ? "enabled" : "disabled"
  }
}

resource "aws_ecs_task_definition" "backend" {
  family                   = "${local.name}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "chroma"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.chroma.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.chroma.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name         = "backend"
    image        = local.backend_image
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = local.backend_environment
    secrets      = local.backend_secrets
    mountPoints  = [{ sourceVolume = "chroma", containerPath = "/data/chroma", readOnly = false }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.backend.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command = ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/health || exit 1"]
      # Generous start period: the container loads the classifier and opens the
      # Chroma index before it can serve.
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])
}

# --- Load balancer -----------------------------------------------------------
resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # The default is 60s, which silently kills the agent-status WebSocket and any
  # request whose model calls take longer than a minute. Both are normal here.
  idle_timeout = 3600

  enable_deletion_protection = var.environment == "production"
}

resource "aws_lb_target_group" "backend" {
  name        = "${local.name}-backend"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # No stickiness needed. WebSocket traces are fanned out through Redis pub/sub
  # rather than held in process memory, so any task can serve any client.
  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # With a certificate, port 80 only redirects.
  dynamic "default_action" {
    for_each = var.acm_certificate_arn == "" ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.backend.arn
    }
  }

  dynamic "default_action" {
    for_each = var.acm_certificate_arn == "" ? [] : [1]
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.acm_certificate_arn == "" ? 0 : 1
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}

# --- Service -----------------------------------------------------------------
resource "aws_ecs_service" "backend" {
  name            = "${local.name}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets = aws_subnet.public[*].id
    # Needed to reach ECR and model APIs without a NAT gateway. Inbound is
    # still restricted to the load balancer by the security group.
    assign_public_ip = true
    security_groups  = [aws_security_group.app.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend.arn
    container_name   = "backend"
    container_port   = 8000
  }

  health_check_grace_period_seconds = 120

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http]

  lifecycle {
    # The image tag is rolled by CI, not by terraform.
    ignore_changes = [task_definition, desired_count]
  }
}

# --- Autoscaling -------------------------------------------------------------
resource "aws_appautoscaling_target" "backend" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.backend.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_capacity
  max_capacity       = var.max_capacity
}

resource "aws_appautoscaling_policy" "requests" {
  name               = "${local.name}-request-scaling"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.backend.service_namespace
  resource_id        = aws_appautoscaling_target.backend.resource_id
  scalable_dimension = aws_appautoscaling_target.backend.scalable_dimension

  target_tracking_scaling_policy_configuration {
    # Requests per target, not CPU. Agent requests spend their time waiting on
    # the model, so CPU stays low even when the service is saturated and would
    # be a poor scaling signal.
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label = join("/", [
        aws_lb.main.arn_suffix,
        aws_lb_target_group.backend.arn_suffix,
      ])
    }
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
