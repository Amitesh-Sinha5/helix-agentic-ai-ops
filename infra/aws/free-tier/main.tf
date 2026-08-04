/**
 * Helix on the AWS free tier — one t3.micro and nothing else.
 *
 * Everything here is inside the 12-month free tier for a new account:
 *
 *   EC2 t3.micro     750 h/month  (one instance running 24/7)
 *   EBS gp3          30 GB
 *   Elastic IP       free while attached to a running instance
 *   Data transfer    100 GB out/month
 *   AWS Budgets      first two budgets free
 *
 * Deliberately NOT used, because none of them are free:
 *   ALB (~$16/mo), Fargate, NAT gateway, RDS beyond the free hours,
 *   ElastiCache beyond the free hours.
 *
 * Postgres and Redis are replaced by SQLite and the backend's in-process
 * cache. On a single instance that is the correct configuration, not a
 * downgrade — see docker-compose.free.yml.
 *
 *   terraform init
 *   terraform apply -var="alert_email=you@example.com"
 *
 * IMPORTANT: the free tier lasts 12 months. After that this instance costs
 * roughly $7.50/month. The budget alarm below emails you long before that
 * becomes a surprise.
 */

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Application = "helix"
      ManagedBy   = "terraform"
      Tier        = "free"
    }
  }
}

# --- The cheapest ARM/x86 image that is free-tier eligible -------------------
# t3.micro is x86, so the AMI must be x86_64. t4g.micro (ARM, cheaper) is only
# free-tier eligible in some regions and periods, so this defaults to the safe
# option.
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-x86_64"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Using the default VPC on purpose: a custom VPC would tempt a NAT gateway,
# which alone costs four times the instance.
resource "aws_security_group" "helix" {
  name        = "helix-free"
  description = "Helix single-instance deployment"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    # Defaults to nothing. Set ssh_cidr to YOUR.IP/32 to enable SSH.
    cidr_blocks = var.ssh_cidr == "" ? [] : [var.ssh_cidr]
  }

  ingress {
    description = "Frontend"
    from_port   = 5173
    to_port     = 5173
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "helix-free" }
}

resource "aws_instance" "helix" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.helix.id]
  key_name               = var.key_name == "" ? null : var.key_name

  root_block_device {
    # 30 GB is the free-tier ceiling. The images and swap file need most of it.
    volume_size           = 30
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  # Require IMDSv2 — the metadata service is how instance credentials leak.
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user-data.sh", {
    repo_url     = var.repo_url
    llm_provider = var.llm_provider
    swap_gb      = var.swap_gb
  })

  tags = { Name = "helix" }
}

# A static address so the CORS origin and the frontend build do not have to
# change every reboot. Free while attached to a running instance.
resource "aws_eip" "helix" {
  instance = aws_instance.helix.id
  domain   = "vpc"
  tags     = { Name = "helix" }
}

# --- Spend guard -------------------------------------------------------------
# "Free" is only free if you find out the moment it stops being free. This
# emails at 1 USD of forecast spend, long before anything meaningful accrues.
resource "aws_budgets_budget" "zero_spend" {
  name         = "helix-zero-spend"
  budget_type  = "COST"
  limit_amount = "1.0"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 1
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
