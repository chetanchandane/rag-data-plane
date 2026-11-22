variable "name_prefix" {
  type = string
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "api_image_tag" {
  description = "Tag of the API image in ECR (the deploy workflow uses the git SHA)."
  type        = string
}

variable "raw_bucket_name" {
  description = "The bucket presigned upload URLs point at."
  type        = string
}

variable "raw_bucket_arn" {
  type = string
}

variable "database_url_param_name" {
  description = "SSM SecureString with the app_api URL (pooled endpoint). Read at cold start."
  type        = string
}

variable "database_url_param_arn" {
  type = string
}

variable "embedding_model_id" {
  description = "Bedrock model for query embeddings: must be the worker's, so vectors are comparable."
  type        = string
}

variable "honeycomb_api_key_param_name" {
  description = "SSM SecureString holding the Honeycomb ingest key. The API reads it at cold start."
  type        = string
}

variable "honeycomb_api_key_param_arn" {
  type = string
}

variable "jwt_issuer" {
  description = "The JWT authorizer's issuer: the tokens' `iss`, no trailing slash. API Gateway finds the JWKS through its discovery document."
  type        = string

  validation {
    condition     = startswith(var.jwt_issuer, "https://") && !endswith(var.jwt_issuer, "/")
    error_message = "jwt_issuer must be an https URL with no trailing slash (it must equal the tokens' iss exactly)."
  }
}

variable "jwt_audience" {
  description = "The tokens' `aud`."
  type        = string
  default     = "rag-data-plane"
}

variable "throttling_rate_limit" {
  description = "Steady-state requests per second across all routes (SPEC: Auth at the edge)."
  type        = number
  default     = 10
}

variable "throttling_burst_limit" {
  type    = number
  default = 20
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
