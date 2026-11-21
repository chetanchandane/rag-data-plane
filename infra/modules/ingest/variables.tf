variable "name_prefix" {
  type = string
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "raw_bucket_name" {
  type = string
}

variable "raw_bucket_arn" {
  type = string
}

variable "worker_image_tag" {
  description = "Tag of the worker image in ECR (the deploy workflow uses the git SHA)."
  type        = string
}

variable "embedding_model_id" {
  type = string
}

variable "database_url_param_name" {
  type = string
}

variable "database_url_param_arn" {
  type = string
}

variable "honeycomb_api_key_param_name" {
  description = "SSM SecureString holding the Honeycomb ingest key. The worker reads it at cold start."
  type        = string
}

variable "honeycomb_api_key_param_arn" {
  type = string
}

variable "honeycomb_endpoint" {
  description = "Honeycomb's OTLP/HTTP endpoint for the account's region."
  type        = string
  default     = "https://api.honeycomb.io"
}

variable "workload_role_path" {
  description = "IAM path the deploy role may create roles under (bootstrap output)."
  type        = string
}

variable "workload_boundary_arn" {
  description = "Permissions boundary every workload role must carry (bootstrap output)."
  type        = string
}

variable "log_retention_days" {
  type = number
}
