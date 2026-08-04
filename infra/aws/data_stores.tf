/**
 * Postgres, Redis and the shared Chroma volume.
 */

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "postgres" {
  identifier     = "${local.name}-pg"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "helix"
  username = var.db_username
  password = var.db_password
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.data.id]
  publicly_accessible    = false

  backup_retention_period = 7
  skip_final_snapshot     = var.environment != "production"
  final_snapshot_identifier = (
    var.environment == "production" ? "${local.name}-final-${formatdate("YYYYMMDDhhmmss", timestamp())}" : null
  )
  deletion_protection = var.environment == "production"

  # A single-AZ instance is a deliberate cost choice for a demo. Set this true
  # for anything real: it roughly doubles the cost and removes the AZ as a
  # single point of failure.
  multi_az = var.db_multi_az

  auto_minor_version_upgrade = true
  apply_immediately          = var.environment != "production"

  lifecycle {
    ignore_changes = [final_snapshot_identifier, engine_version]
  }
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${local.name}-redis"
  description          = "Helix semantic cache, rate limits, sessions and pub/sub"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = var.redis_node_type

  num_cache_clusters         = var.redis_multi_az ? 2 : 1
  automatic_failover_enabled = var.redis_multi_az
  multi_az_enabled           = var.redis_multi_az

  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.data.id]
  at_rest_encryption_enabled = true

  # TLS is left off so the connection string stays `redis://`. Turn it on and
  # switch REDIS_URL to `rediss://` for anything handling real user data.
  transit_encryption_enabled = false

  # Everything Helix keeps in Redis is reconstructible and TTL'd: the semantic
  # cache, rate-limit windows, session memory and the trace replay buffer.
  # Evicting the least-recently-used key under pressure is correct behaviour;
  # refusing writes (the default `noeviction`) would break rate limiting.
  parameter_group_name = aws_elasticache_parameter_group.redis.name

  apply_immediately = var.environment != "production"
}

resource "aws_elasticache_parameter_group" "redis" {
  name   = "${local.name}-redis-params"
  family = "redis7"

  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }
}

# --- Chroma index ------------------------------------------------------------
# EFS rather than a task-local volume because every backend task must see the
# same index. This is the AWS equivalent of the ReadWriteMany PVC in the
# Kubernetes manifests, and it is the reason the service can run more than one
# replica at all.
resource "aws_efs_file_system" "chroma" {
  creation_token = "${local.name}-chroma"
  encrypted      = true

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${local.name}-chroma" }
}

resource "aws_efs_mount_target" "chroma" {
  count           = length(aws_subnet.private)
  file_system_id  = aws_efs_file_system.chroma.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.data.id]
}

resource "aws_efs_access_point" "chroma" {
  file_system_id = aws_efs_file_system.chroma.id

  # Matches the non-root `helix` user (uid 10001) the backend image runs as.
  posix_user {
    uid = 10001
    gid = 10001
  }

  root_directory {
    path = "/chroma"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0755"
    }
  }

  tags = { Name = "${local.name}-chroma-ap" }
}

# --- Secrets -----------------------------------------------------------------
resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/app-secrets"
  recovery_window_in_days = var.environment == "production" ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    DATABASE_URL = join("", [
      "postgresql+asyncpg://", var.db_username, ":", var.db_password,
      "@", aws_db_instance.postgres.address, ":5432/helix",
    ])
    REDIS_URL             = "redis://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
    JWT_SECRET_KEY        = var.jwt_secret
    ANTHROPIC_API_KEY     = var.anthropic_api_key
    OPENAI_API_KEY        = var.openai_api_key
    STRIPE_SECRET_KEY     = var.stripe_secret_key
    STRIPE_WEBHOOK_SECRET = var.stripe_webhook_secret
  })
}
