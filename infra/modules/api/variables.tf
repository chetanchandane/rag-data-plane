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

variable "llm_primary_model_id" {
  description = "Primary LLM: a US cross-region inference profile ID, called through Converse."
  type        = string

  validation {
    condition     = startswith(var.llm_primary_model_id, "us.")
    error_message = "Use a us.* inference profile, so inference stays in US Regions."
  }
}

variable "llm_secondary_model_id" {
  description = "Secondary LLM, used when the primary fails: a us.* inference profile ID."
  type        = string

  validation {
    condition     = startswith(var.llm_secondary_model_id, "us.")
    error_message = "Use a us.* inference profile, so inference stays in US Regions."
  }
}

variable "llm_inference_regions" {
  description = "Regions the us.* profiles route requests to (aws bedrock list-inference-profiles)."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}

variable "llm_primary_effort" {
  description = "Claude's effort level (low, medium or high)."
  type        = string
  default     = "low"
}

variable "rerank_model_id" {
  description = "Cohere rerank model."
  type        = string
  default     = "rerank-v5.0-fast"
}

variable "cohere_api_key_param_name" {
  description = "SSM SecureString holding the Cohere Production key. Read at cold start."
  type        = string
}

variable "cohere_api_key_param_arn" {
  type = string
}

variable "abstain_threshold" {
  description = "Top rerank score below which /v1/query abstains without calling the LLM."
  type        = number
  default     = 0.3
}

variable "llm_context_chunks" {
  description = "Reranked chunks sent to the LLM (SPEC: rerank top 5)."
  type        = number
  default     = 5
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
