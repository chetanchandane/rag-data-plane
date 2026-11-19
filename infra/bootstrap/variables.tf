variable "account_id" {
  description = "The only AWS account this stack may touch (DECISIONS: AWS account is 905418224080)."
  type        = string
}

variable "region" {
  description = "Region for the state bucket and every resource the deploy role manages."
  type        = string
}

variable "github_repo" {
  description = "owner/name of the GitHub repository whose deploys the role trusts."
  type        = string
}

variable "github_environment" {
  description = "The GitHub Actions environment allowed to assume the deploy role."
  type        = string
}

variable "name_prefix" {
  description = "Prefix of every resource name the deploy role may manage."
  type        = string
  default     = "ragdp"
}
