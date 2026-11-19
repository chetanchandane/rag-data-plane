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

variable "worker_image_tag" {
  description = "Worker image tag in ECR. The deploy workflow passes the git SHA."
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
