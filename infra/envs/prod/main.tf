# Production stack, Slice 1: raw bucket, ingest path, secrets names, alarms and budget.
# Applied by GitHub Actions through the bootstrap deploy role, or by Chetan from the laptop.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id] # never deploy into another account

  default_tags {
    tags = {
      Project     = var.name_prefix
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}

locals {
  # Bootstrap outputs, rebuilt from their fixed names so this stack doesn't read
  # bootstrap's state (the deploy role can't).
  workload_role_path    = "/${var.name_prefix}/"
  workload_boundary_arn = "arn:aws:iam::${var.account_id}:policy/${var.name_prefix}/${var.name_prefix}-workload-boundary"
}

module "storage" {
  source              = "../../modules/storage"
  bucket_name         = "${var.name_prefix}-raw-prod-${var.account_id}" # bucket names are global
  upload_cors_origins = var.upload_cors_origins
}

module "secrets" {
  source      = "../../modules/secrets"
  name_prefix = var.name_prefix
  environment = "prod"
  region      = var.region
  account_id  = var.account_id
}

module "ingest" {
  source                  = "../../modules/ingest"
  name_prefix             = var.name_prefix
  region                  = var.region
  account_id              = var.account_id
  raw_bucket_name         = module.storage.bucket_name
  raw_bucket_arn          = module.storage.bucket_arn
  worker_image_tag        = var.worker_image_tag
  embedding_model_id      = var.embedding_model_id
  database_url_param_name = module.secrets.names.database_url_ingest
  database_url_param_arn  = module.secrets.arns.database_url_ingest
  workload_role_path      = local.workload_role_path
  workload_boundary_arn   = local.workload_boundary_arn
  log_retention_days      = 14
}

module "observability" {
  source      = "../../modules/observability"
  name_prefix = var.name_prefix
  dlq_name    = module.ingest.dlq_name
  alert_email = var.alert_email
}

output "raw_bucket" {
  value = module.storage.bucket_name
}

output "ingest_queue_url" {
  value = module.ingest.queue_url
}

output "dlq_url" {
  value = module.ingest.dlq_url
}

output "worker_repository_url" {
  value = module.ingest.worker_repository_url
}
