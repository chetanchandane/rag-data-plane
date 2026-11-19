variable "bucket_name" {
  description = "Raw bucket name. Must start with the deploy role's allowed prefix (ragdp-raw-)."
  type        = string
}

variable "upload_cors_origins" {
  description = "Browser origins allowed to upload with presigned PUTs. Empty: no CORS rule."
  type        = list(string)
  default     = []
}
