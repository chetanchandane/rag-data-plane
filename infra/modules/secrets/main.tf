# SSM parameter names (SPEC: Infrastructure, "secrets"). Names only: the parameters and their
# values are created out of band (see infra/envs/prod/README.md), so no secret ever passes
# through Terraform or lands in its state file, and the deploy role never needs to read them.

locals {
  prefix = "/${var.name_prefix}/${var.environment}"

  names = {
    database_url_ingest = "${local.prefix}/database_url_ingest" # app_ingest, pooled endpoint
  }
}

output "names" {
  description = "Parameter names, for Lambda environment variables."
  value       = local.names
}

output "arns" {
  description = "Parameter ARNs, for IAM policies."
  value = {
    for key, name in local.names :
    key => "arn:aws:ssm:${var.region}:${var.account_id}:parameter${name}"
  }
}
