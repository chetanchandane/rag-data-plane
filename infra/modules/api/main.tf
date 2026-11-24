# The API (SPEC: Infrastructure, "Auth at the edge"; DECISIONS 2026-11-21, "Step 13"):
# client -> HTTP API (JWT authorizer, route scopes, throttling, access logs) -> API Lambda
# (FastAPI via Mangum).
#
# A request with a missing, expired or otherwise invalid token gets a 401 from API Gateway
# and never invokes the Lambda. So do paths without a route: they get a 404 there.
locals {
  p           = var.name_prefix
  api_name    = "${var.name_prefix}-api"
  api_log     = "/aws/lambda/${var.name_prefix}-api"
  api_log_arn = "arn:aws:logs:${var.region}:${var.account_id}:log-group:${local.api_log}"

  # Each LLM is either a US cross-region inference profile (us.*), which sends a request to
  # the same model in any of llm_inference_regions, or a model called directly in this
  # Region (no prefix). DECISIONS 2026-11-24, "LLMs without Anthropic".
  llm_ids         = [var.llm_primary_model_id, var.llm_secondary_model_id]
  llm_profile_ids = [for id in local.llm_ids : id if startswith(id, "us.")]
  llm_direct_ids  = [for id in local.llm_ids : id if !startswith(id, "us.")]
  llm_profile_arns = [
    for id in local.llm_profile_ids :
    "arn:aws:bedrock:${var.region}:${var.account_id}:inference-profile/${id}"
  ]
  llm_routed_model_arns = flatten([
    for id in local.llm_profile_ids : [
      for region in var.llm_inference_regions :
      "arn:aws:bedrock:${region}::foundation-model/${trimprefix(id, "us.")}"
    ]
  ])
  llm_direct_model_arns = [
    for id in local.llm_direct_ids : "arn:aws:bedrock:${var.region}::foundation-model/${id}"
  ]

  # Route -> the scopes a token needs (any one of them). Coarse API permissions at the edge;
  # which rows a caller sees is still decided by role_level and RLS. whoami needs only a
  # valid token.
  routes = {
    "GET /v1/whoami"         = []
    "POST /v1/documents"     = ["documents:write"]
    "GET /v1/documents/{id}" = ["documents:read"]
    "POST /v1/retrieve"      = ["retrieve"] # its own scope: /v1/query spends LLM money
    "POST /v1/query"         = ["query"]
  }
}

# --- Image repository ----------------------------------------------------------------------

resource "aws_ecr_repository" "api" {
  name                 = local.api_name
  image_tag_mutability = "IMMUTABLE" # a tag always means the same image (deploys use the git SHA)

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 3 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

# --- API role ------------------------------------------------------------------------------

data "aws_iam_policy_document" "api_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name                 = local.api_name
  path                 = var.workload_role_path
  assume_role_policy   = data.aws_iam_policy_document.api_trust.json
  permissions_boundary = var.workload_boundary_arn # required by the deploy role's policy
}

# Exactly what the API does, on exactly these resources. The boundary caps it further.
data "aws_iam_policy_document" "api" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.api_log_arn}:*"]
  }

  statement {
    sid       = "ReadDatabaseUrl"
    actions   = ["ssm:GetParameter"]
    resources = [var.database_url_param_arn]
  }

  statement {
    sid       = "DecryptViaSsmOnly"
    actions   = ["kms:Decrypt"]
    resources = ["*"] # the AWS-managed SSM key; usable only through SSM (condition below)
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "ReadTracingKey"
    actions   = ["ssm:GetParameter"]
    resources = [var.honeycomb_api_key_param_arn]
  }

  # The query's embedding: the same single model the worker embeds chunks with.
  statement {
    sid       = "EmbedQueriesWithOneModel"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"]
  }

  # Answers (step 15). An LLM behind a US inference profile needs the profile itself and the
  # model in every Region it may route to; the condition allows those models only through
  # these profiles, never called directly. Skipped when neither LLM is a profile.
  dynamic "statement" {
    for_each = length(local.llm_profile_ids) > 0 ? [1] : []
    content {
      sid       = "GenerateThroughInferenceProfiles"
      actions   = ["bedrock:InvokeModel"]
      resources = local.llm_profile_arns
    }
  }

  dynamic "statement" {
    for_each = length(local.llm_profile_ids) > 0 ? [1] : []
    content {
      sid       = "GenerateOnRoutedModels"
      actions   = ["bedrock:InvokeModel"]
      resources = local.llm_routed_model_arns
      condition {
        test     = "StringLike"
        variable = "bedrock:InferenceProfileArn"
        values   = local.llm_profile_arns
      }
    }
  }

  # An LLM called directly: only that model, only in this Region.
  dynamic "statement" {
    for_each = length(local.llm_direct_ids) > 0 ? [1] : []
    content {
      sid       = "GenerateOnModelsInRegion"
      actions   = ["bedrock:InvokeModel"]
      resources = local.llm_direct_model_arns
    }
  }

  statement {
    sid       = "ReadRerankKey"
    actions   = ["ssm:GetParameter"]
    resources = [var.cohere_api_key_param_arn]
  }

  # Presigned upload URLs are signed with this role's credentials, so S3 checks this
  # permission when the client uploads. Uploads go under raw/ only.
  statement {
    sid       = "PresignRawUploads"
    actions   = ["s3:PutObject"]
    resources = ["${var.raw_bucket_arn}/raw/*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

# --- API Lambda ----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = local.api_log
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "api" {
  function_name = local.api_name
  role          = aws_iam_role.api.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
  architectures = ["x86_64"] # images are built for linux/amd64

  # /v1/query's worst case: embedding, SQL, reranker, then both LLMs (10 s each, api/llm.py).
  # Just under API Gateway's 30 s integration timeout, so the Lambda gives up first.
  timeout     = 29
  memory_size = 1024 # Lambda CPU scales with memory: a faster cold start for pennies

  # No reserved concurrency: the account limit of 10 allows none (DECISIONS, step 8). Route
  # throttling on the stage caps the request rate instead.

  # Names only. The DB URL and the Honeycomb and Cohere keys are read from SSM at cold start,
  # never put here. AWS_REGION is set by Lambda. No JWT_ISSUER: API Gateway verifies tokens
  # (api/auth.py).
  environment {
    variables = {
      RAW_BUCKET              = var.raw_bucket_name
      DATABASE_URL_API_PARAM  = var.database_url_param_name
      EMBEDDING_MODEL_ID      = var.embedding_model_id
      HONEYCOMB_API_KEY_PARAM = var.honeycomb_api_key_param_name
      COHERE_API_KEY_PARAM    = var.cohere_api_key_param_name
      RERANK_MODEL_ID         = var.rerank_model_id
      LLM_PRIMARY_MODEL_ID    = var.llm_primary_model_id
      LLM_SECONDARY_MODEL_ID  = var.llm_secondary_model_id
      LLM_PRIMARY_EFFORT      = var.llm_primary_effort
      ABSTAIN_THRESHOLD       = tostring(var.abstain_threshold)
      LLM_CONTEXT_CHUNKS      = tostring(var.llm_context_chunks)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.api.name
  }

  depends_on = [aws_iam_role_policy.api]
}

# --- HTTP API ------------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "api" {
  name          = local.api_name
  description   = "rag-data-plane API"
  protocol_type = "HTTP"
  # No CORS: there is no browser client (SPEC: Non-goals, UI).
}

# API Gateway fetches the issuer's discovery document, then its JWKS, and checks each token's
# signature (RSA only), kid, iss, aud, exp, nbf and iat before the Lambda runs.
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.api.id
  name             = "${local.p}-jwt"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = var.jwt_issuer
    audience = [var.jwt_audience]
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0" # the event shape api/auth.py and Mangum expect
}

resource "aws_apigatewayv2_route" "api" {
  for_each = local.routes

  api_id               = aws_apigatewayv2_api.api.id
  route_key            = each.key
  target               = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.jwt.id
  authorization_scopes = length(each.value) > 0 ? each.value : null
}

resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${local.api_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.throttling_rate_limit
    throttling_burst_limit = var.throttling_burst_limit
  }

  # One JSON line per request. authorizerError and integrationStatus show whether a refused
  # request ever reached the Lambda (an empty integrationStatus means it didn't). Claims are
  # ids only; the token itself is never logged.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = jsonencode({
      requestId          = "$context.requestId"
      requestTime        = "$context.requestTime"
      sourceIp           = "$context.identity.sourceIp"
      routeKey           = "$context.routeKey"
      status             = "$context.status"
      responseLength     = "$context.responseLength"
      responseLatency    = "$context.responseLatency"
      authorizerError    = "$context.authorizer.error"
      integrationStatus  = "$context.integration.integrationStatus"
      integrationLatency = "$context.integration.latency"
      integrationError   = "$context.integration.error"
      sub                = "$context.authorizer.claims.sub"
      tenantId           = "$context.authorizer.claims.tenant_id"
      jti                = "$context.authorizer.claims.jti"
    })
  }
}

# Only this API may invoke the function (any stage, method and route of it). This is what
# makes trusting the event's claims safe (api/auth.py).
resource "aws_lambda_permission" "http_api" {
  statement_id  = "AllowThisHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
