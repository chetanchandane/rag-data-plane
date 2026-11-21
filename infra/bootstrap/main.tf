# Bootstrap (SPEC: Infrastructure). Applied once, by hand, from Chetan's laptop. It creates what
# CI needs before CI can deploy anything:
#   1. the S3 bucket that holds Terraform state (S3 native locking, no DynamoDB table)
#   2. the GitHub Actions OIDC provider, so CI gets short-lived credentials and no access keys
#   3. the deploy role, which only this repo's `prod` environment can assume
#   4. a permissions boundary that caps every role the deploy role creates
#
# Its own state starts local; see README.md for moving it into the bucket after the first apply.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Refuses to run against any other account. Signing in to the wrong account is easy here
  # (one sign-in reaches several accounts), and this turns that into an error instead of a deploy.
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      Project   = var.name_prefix
      Stack     = "bootstrap"
      ManagedBy = "terraform"
    }
  }
}

locals {
  account = var.account_id
  region  = var.region
  p       = var.name_prefix

  # Roles the deploy role creates (Lambda execution roles) live under this path. The deploy
  # role itself is at "/", so nothing it can create or edit matches its own ARN.
  workload_role_path = "/${var.name_prefix}/"
  workload_roles_arn = "arn:aws:iam::${var.account_id}:role/${var.name_prefix}/*"

  state_key_prod = "envs/prod/terraform.tfstate"
}

# --- 1. State bucket -----------------------------------------------------------------------

resource "aws_s3_bucket" "state" {
  # Bucket names are global; the account id keeps this one unique.
  bucket = "${local.p}-tfstate-${local.account}"

  lifecycle {
    prevent_destroy = true # losing state means Terraform forgets every resource it made
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled" # a bad apply can be rolled back to the previous state file
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3: no KMS key to pay for or manage
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced" # ACLs off; access is decided by IAM only
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "state_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_bucket.json

  # The policy can only be attached once public-access settings exist.
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- 2. GitHub OIDC provider ---------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint_list: AWS verifies GitHub's certificate against its trusted CAs.
}

# --- 3. Deploy role ------------------------------------------------------------------------

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Exact match, no wildcard: only jobs that run in this repo's `prod` environment. Any job
    # that names that environment gets this `sub`, whatever its branch, so GitHub's deployment
    # branch policy (prod: `main` only) is what keeps other branches and pull requests out;
    # forks get a different `sub` and are refused. The repo uses
    # GitHub's immutable subject (numeric owner and repo IDs, not just names), so a repo
    # recreated under the same name can't assume these roles either.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_subject_prefix}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${local.p}-github-deploy"
  description          = "Assumed by GitHub Actions (${var.github_repo}, environment ${var.github_environment}) to run terraform apply for infra/envs/prod."
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

# What `terraform apply` in infra/envs/prod needs (storage, ingest, observability, issuer,
# api). Every statement is limited to the project's resources in this account: by name prefix
# where the resource has a name, by the Project tag where it has only a random id. The
# exceptions are said where they are (origin access controls, HTTP API changes, log
# delivery).
data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["envs/prod/*"]
    }
  }

  statement {
    sid     = "StateReadWrite"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    # The state file and its S3-native lock file. Bootstrap's own state is not reachable.
    resources = [
      "${aws_s3_bucket.state.arn}/${local.state_key_prod}",
      "${aws_s3_bucket.state.arn}/${local.state_key_prod}.tflock",
    ]
  }

  statement {
    sid       = "RawBucket"
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::${local.p}-raw-*", "arn:aws:s3:::${local.p}-raw-*/*"]
  }

  # The token issuer's bucket (step 11): private, read only by its CloudFront distribution.
  statement {
    sid       = "IssuerBucket"
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::${local.p}-issuer-*", "arn:aws:s3:::${local.p}-issuer-*/*"]
  }

  # CloudFront distributions have random ids, so they are scoped by tag. Creating one requires
  # the Project tag in the request (the provider's default_tags send it); everything after
  # that requires the tag on the distribution. TagResource is in both, because the tags are
  # attached during the create call.
  statement {
    sid       = "CreateProjectDistributions"
    actions   = ["cloudfront:CreateDistribution", "cloudfront:TagResource"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [local.p]
    }
  }

  statement {
    sid = "ManageProjectDistributions"
    actions = [
      "cloudfront:GetDistribution",
      "cloudfront:GetDistributionConfig",
      "cloudfront:UpdateDistribution",
      "cloudfront:DeleteDistribution",
      "cloudfront:ListTagsForResource",
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
      "cloudfront:CreateInvalidation",
      "cloudfront:GetInvalidation",
    ]
    resources = ["arn:aws:cloudfront::${local.account}:distribution/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [local.p]
    }
  }

  # Origin access controls have random ids, no tags and no condition keys, so this can't be
  # narrower than the account. An OAC grants nothing by itself: a bucket policy has to name
  # the distribution that uses it.
  statement {
    sid = "OriginAccessControls"
    actions = [
      "cloudfront:CreateOriginAccessControl",
      "cloudfront:GetOriginAccessControl",
      "cloudfront:GetOriginAccessControlConfig",
      "cloudfront:UpdateOriginAccessControl",
      "cloudfront:DeleteOriginAccessControl",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "Queues"
    actions   = ["sqs:*"]
    resources = ["arn:aws:sqs:${local.region}:${local.account}:${local.p}-*"]
  }

  statement {
    sid       = "EventRules"
    actions   = ["events:*"]
    resources = ["arn:aws:events:${local.region}:${local.account}:rule/${local.p}-*"]
  }

  statement {
    sid       = "ImageRepos"
    actions   = ["ecr:*"]
    resources = ["arn:aws:ecr:${local.region}:${local.account}:repository/${local.p}-*"]
  }

  statement {
    sid       = "Functions"
    actions   = ["lambda:*"]
    resources = ["arn:aws:lambda:${local.region}:${local.account}:function:${local.p}-*"]
  }

  # Event source mappings have random ids, so they are scoped by the function they feed.
  statement {
    sid = "EventSourceMappings"
    actions = [
      "lambda:CreateEventSourceMapping",
      "lambda:GetEventSourceMapping",
      "lambda:UpdateEventSourceMapping",
      "lambda:DeleteEventSourceMapping",
    ]
    resources = ["*"]
    condition {
      test     = "ArnLike"
      variable = "lambda:FunctionArn"
      values   = ["arn:aws:lambda:${local.region}:${local.account}:function:${local.p}-*"]
    }
  }

  # Terraform reads and writes the mapping's tags (default_tags) by the mapping's own ARN,
  # which has a random id and no condition key naming its function. Tag calls only.
  statement {
    sid       = "EventSourceMappingTags"
    actions   = ["lambda:ListTags", "lambda:TagResource", "lambda:UntagResource"]
    resources = ["arn:aws:lambda:${local.region}:${local.account}:event-source-mapping:*"]
  }

  statement {
    sid     = "LogGroups"
    actions = ["logs:*"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/lambda/${local.p}-*",
      "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/lambda/${local.p}-*:*",
      # The HTTP API's access logs (step 13).
      "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/apigateway/${local.p}-*",
      "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/apigateway/${local.p}-*:*",
    ]
  }

  # API Gateway delivers the HTTP API's access logs as the principal that configures them,
  # which needs these (AWS: "Configuring logging for an HTTP API"). AWS allows no resource
  # scoping for them.
  statement {
    sid = "ApiAccessLogDelivery"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
    ]
    resources = ["*"]
  }

  # HTTP APIs (step 13, DECISIONS 2026-11-21). Creating one requires the Project tag, which
  # default_tags sets. Changing an API, its stages, routes, integrations and authorizers is
  # allowed on any HTTP API in this Region: AWS doesn't document that those sub-resources
  # carry the API's tags, and this account runs only this project.
  statement {
    sid       = "CreateProjectHttpApis"
    actions   = ["apigateway:POST"]
    resources = ["arn:aws:apigateway:${local.region}::/apis"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [local.p]
    }
  }

  statement {
    sid = "ManageHttpApis"
    actions = [
      "apigateway:GET",
      "apigateway:POST",
      "apigateway:PUT",
      "apigateway:PATCH",
      "apigateway:DELETE",
      # Creating a stage with tags is also authorized as TagResource (the first deploy was
      # refused without it), though AWS's IAM reference maps tagging to the verbs above.
      "apigateway:TagResource",
      "apigateway:UntagResource",
    ]
    resources = [
      "arn:aws:apigateway:${local.region}::/apis/*",
      "arn:aws:apigateway:${local.region}::/tags/*",
    ]
  }

  statement {
    sid       = "Alarms"
    actions   = ["cloudwatch:*"]
    resources = ["arn:aws:cloudwatch:${local.region}:${local.account}:alarm:${local.p}-*"]
  }

  statement {
    sid       = "AlarmTopics"
    actions   = ["sns:*"]
    resources = ["arn:aws:sns:${local.region}:${local.account}:${local.p}-*"]
  }

  statement {
    sid = "Budgets"
    actions = [
      "budgets:ViewBudget",
      "budgets:ModifyBudget",
      "budgets:ListTagsForResource",
      "budgets:TagResource",
      "budgets:UntagResource",
    ]
    resources = ["arn:aws:budgets::${local.account}:budget/${local.p}-*"]
  }

  # No SSM access: parameters and their values are created out of band (modules/secrets), so
  # the deploy role never sees a secret.

  # Read and list calls that AWS doesn't allow to be scoped to a resource.
  statement {
    sid = "ListAndDescribe"
    actions = [
      "ecr:GetAuthorizationToken",
      "logs:DescribeLogGroups",
      "sns:ListTopics",
      "sqs:ListQueues",
      "events:ListRules",
      "lambda:ListEventSourceMappings",
      "lambda:GetAccountSettings",
      "cloudwatch:DescribeAlarms",
      "cloudfront:ListCachePolicies", # the issuer looks up the managed CachingOptimized policy
      "cloudfront:GetCachePolicy",
    ]
    resources = ["*"]
  }

  # IAM: the deploy role may manage Lambda execution roles, but only under the workload path
  # and only with the workload boundary attached. That boundary is what stops it from
  # creating a role more powerful than itself (privilege escalation).
  statement {
    sid = "WorkloadRolesWithBoundary"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:UpdateAssumeRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePermissionsBoundary",
    ]
    resources = [local.workload_roles_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.workload_boundary.arn]
    }
  }

  statement {
    sid = "WorkloadRolesRead"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRoleTags",
      "iam:TagRole",
      "iam:UntagRole",
    ]
    resources = [local.workload_roles_arn]
  }

  statement {
    sid       = "PassWorkloadRolesToLambdaOnly"
    actions   = ["iam:PassRole"]
    resources = [local.workload_roles_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  statement {
    sid       = "ReadPolicies"
    actions   = ["iam:GetPolicy", "iam:GetPolicyVersion"]
    resources = [aws_iam_policy.workload_boundary.arn, "arn:aws:iam::aws:policy/*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "terraform-apply-prod"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# --- 3b. Migrate role (step 10c) -----------------------------------------------------------

# CI's production migrate job: reads the app_owner URL from SSM and runs Alembic. It can do
# nothing else in AWS. Same trust as the deploy role (this repo's `prod` environment only).
# A job's credentials are then only what that job needs, though both roles trust the same
# `sub`, so this separates credentials per job rather than drawing a hard boundary.
resource "aws_iam_role" "migrate" {
  name                 = "${local.p}-github-migrate"
  description          = "Assumed by GitHub Actions (${var.github_repo}, environment ${var.github_environment}) to run database migrations on production."
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

locals {
  owner_url_parameter_arn = "arn:aws:ssm:${local.region}:${local.account}:parameter/${local.p}/prod/database_url_owner"
}

data "aws_iam_policy_document" "migrate" {
  statement {
    sid       = "ReadOwnerUrl"
    actions   = ["ssm:GetParameter"]
    resources = [local.owner_url_parameter_arn]
  }

  # The parameter is a SecureString under the AWS-managed SSM key. Decrypt only through SSM,
  # and only for this one parameter (SSM puts its ARN in the encryption context).
  statement {
    sid       = "DecryptOwnerUrlViaSsm"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:PARAMETER_ARN"
      values   = [local.owner_url_parameter_arn]
    }
  }
}

resource "aws_iam_role_policy" "migrate" {
  name   = "read-owner-url"
  role   = aws_iam_role.migrate.id
  policy = data.aws_iam_policy_document.migrate.json
}

# --- 4. Workload permissions boundary ------------------------------------------------------

# The most any Lambda role made by the deploy role can ever do. Each role still gets its own,
# narrower policy in infra/modules; this only caps it. Only this bootstrap (run by Chetan)
# can change it: the deploy role can read it but not edit it.
data "aws_iam_policy_document" "workload_boundary" {
  statement {
    sid     = "OwnLogs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/lambda/${local.p}-*:*",
    ]
  }

  statement {
    sid = "Queues"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
      "sqs:SendMessage",
    ]
    resources = ["arn:aws:sqs:${local.region}:${local.account}:${local.p}-*"]
  }

  statement {
    sid       = "RawObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.p}-raw-*/*"]
  }

  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${local.region}:${local.account}:parameter/${local.p}/prod/*"]
  }

  # SecureString parameters use the AWS-managed SSM key; decrypting is allowed only via SSM.
  statement {
    sid       = "DecryptParametersViaSsm"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "InvokeBedrockModels"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${local.region}::foundation-model/*"]
  }
}

resource "aws_iam_policy" "workload_boundary" {
  name        = "${local.p}-workload-boundary"
  path        = local.workload_role_path
  description = "Permissions boundary for every role the GitHub deploy role creates."
  policy      = data.aws_iam_policy_document.workload_boundary.json
}
