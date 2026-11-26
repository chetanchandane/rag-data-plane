variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name_prefix" {
  type    = string
  default = "ragdp"
}

variable "embedding_model_id" {
  type = string
}

variable "llm_primary_model_id" {
  description = "Primary LLM: a us.* inference profile or an in-Region model id."
  type        = string
}

variable "llm_secondary_model_id" {
  description = "Secondary LLM: a us.* inference profile or an in-Region model id."
  type        = string
}

variable "worker_image_tag" {
  description = "Worker image tag in ECR. The deploy workflow passes the git SHA."
  type        = string
}

variable "api_image_tag" {
  description = "API image tag in ECR. The deploy workflow passes the git SHA."
  type        = string
}

variable "upload_cors_origins" {
  description = "Browser origins allowed to upload with presigned PUTs."
  type        = list(string)
}

variable "alert_email" {
  description = "Alarm and budget email. Kept out of the repo: set TF_VAR_alert_email."
  type        = string
}

# The stage throttle (SPEC: Auth at the edge). Defaults, not prod.tfvars: deploy.yml passes no
# value, so every push to main puts it back to 10/20. The load window raises it from the
# laptop with -var (docs/runbooks/load-test.md; DECISIONS 2026-11-25, "Slice 4 step list").
variable "api_throttle_rate_limit" {
  description = "API stage steady-state requests per second, across all routes."
  type        = number
  default     = 10

  validation {
    condition     = var.api_throttle_rate_limit > 0
    error_message = "A rate of 0 makes the stage reject every request."
  }
}

variable "api_throttle_burst_limit" {
  description = "API stage burst (token bucket size)."
  type        = number
  default     = 20

  # Raised and lowered as a pair (10/20, 25/50): a -var forgotten for one of them shows up
  # here instead of as a different 429 profile in the load test.
  validation {
    condition     = var.api_throttle_burst_limit >= var.api_throttle_rate_limit
    error_message = "Set the burst to at least the rate (10/20 normally, 25/50 for the load window)."
  }
}
