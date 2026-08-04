/**
 * Helix on AWS — ECS Fargate.
 *
 * Why Fargate and not Lambda: Helix needs a long-lived process. It holds
 * WebSocket connections open for the life of a request (and indefinitely for
 * admin escalation feeds), keeps a Chroma index on disk, and runs a LangGraph
 * state machine across 5-9 model calls inside a single request. A serverless
 * timeout mid-graph loses the work.
 *
 *   terraform init
 *   terraform plan  -var="db_password=..." -var="jwt_secret=..."
 *   terraform apply -var="db_password=..." -var="jwt_secret=..."
 *
 * Read infra/aws/README.md for the cost breakdown before applying — this
 * provisions billable resources.
 */

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment once a state bucket exists. Local state is fine for a demo and
  # wrong for anything shared.
  # backend "s3" {
  #   bucket       = "helix-tfstate"
  #   key          = "helix/terraform.tfstate"
  #   region       = "ap-south-1"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Application = "helix"
      ManagedBy   = "terraform"
      Environment = var.environment
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = var.name_prefix
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
}
