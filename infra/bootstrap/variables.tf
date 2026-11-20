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

variable "github_subject_prefix" {
  description = "Start of the OIDC `sub` claim for github_repo. This repo uses GitHub's immutable subjects (owner@owner_id/name@repo_id), so a deleted and recreated repo with the same name gets a different `sub`. Get the IDs with: gh api repos/OWNER/NAME --jq '.owner.id, .id'."
  type        = string

  validation {
    condition     = can(regex("^repo:[^/@]+@[0-9]+/[^/@]+@[0-9]+$", var.github_subject_prefix))
    error_message = "Must look like repo:OWNER@OWNER_ID/NAME@REPO_ID."
  }
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
