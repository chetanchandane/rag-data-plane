output "state_bucket" {
  description = "For infra/envs/prod/backend.tf."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "For the deploy workflow's aws-actions/configure-aws-credentials step."
  value       = aws_iam_role.deploy.arn
}

output "workload_boundary_arn" {
  description = "Every role the deploy role creates must carry this permissions boundary."
  value       = aws_iam_policy.workload_boundary.arn
}

output "workload_role_path" {
  description = "Every role the deploy role creates must live under this IAM path."
  value       = local.workload_role_path
}

output "migrate_role_arn" {
  description = "For the CI workflow's production migrate job."
  value       = aws_iam_role.migrate.arn
}
