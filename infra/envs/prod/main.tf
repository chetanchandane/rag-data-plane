# Production stack: raw bucket, ingest path, secrets names, alarms and budget (Slice 1), the
# token issuer (step 11) and the API (step 13).
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
  # Tracing (step 10e). The parameter is created out of band, like the DB URLs.
  honeycomb_api_key_param_name = module.secrets.names.honeycomb_api_key
  honeycomb_api_key_param_arn  = module.secrets.arns.honeycomb_api_key
  workload_role_path           = local.workload_role_path
  workload_boundary_arn        = local.workload_boundary_arn
  log_retention_days           = 14
}

# The JWKS is committed; scripts/issuer.py writes it when keys are created or retired.
module "issuer" {
  source      = "../../modules/issuer"
  bucket_name = "${var.name_prefix}-issuer-prod-${var.account_id}"
  jwks_json   = file("${path.module}/jwks.json")
}

# The API Lambda behind an HTTP API whose JWT authorizer trusts the issuer above.
module "api" {
  source                  = "../../modules/api"
  name_prefix             = var.name_prefix
  region                  = var.region
  account_id              = var.account_id
  api_image_tag           = var.api_image_tag
  raw_bucket_name         = module.storage.bucket_name
  raw_bucket_arn          = module.storage.bucket_arn
  ingest_queue_url        = module.ingest.queue_url # delete jobs (step 21)
  ingest_queue_arn        = module.ingest.queue_arn
  database_url_param_name = module.secrets.names.database_url_api
  database_url_param_arn  = module.secrets.arns.database_url_api
  jwt_issuer              = module.issuer.issuer_url
  embedding_model_id      = var.embedding_model_id
  # Tracing (step 14): the same key as the worker's, dataset ragdp-api.
  honeycomb_api_key_param_name = module.secrets.names.honeycomb_api_key
  honeycomb_api_key_param_arn  = module.secrets.arns.honeycomb_api_key
  # Answers (step 15): Cohere rerank, then the LLMs through US inference profiles.
  cohere_api_key_param_name = module.secrets.names.cohere_api_key
  cohere_api_key_param_arn  = module.secrets.arns.cohere_api_key
  llm_primary_model_id      = var.llm_primary_model_id
  llm_secondary_model_id    = var.llm_secondary_model_id
  workload_role_path        = local.workload_role_path
  workload_boundary_arn     = local.workload_boundary_arn
  log_retention_days        = 14
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

output "api_url" {
  description = "Base URL of the API, e.g. $API_URL/v1/whoami."
  value       = module.api.api_url
}

output "api_repository_url" {
  value = module.api.api_repository_url
}

output "api_access_log_group" {
  value = module.api.access_log_group
}

output "issuer_url" {
  description = "JWT_ISSUER for scripts/issuer.py mint, and the JWT authorizer's issuer."
  value       = module.issuer.issuer_url
}

output "issuer_distribution_id" {
  value = module.issuer.distribution_id
}
