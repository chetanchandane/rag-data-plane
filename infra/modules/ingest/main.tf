# Ingest path (SPEC: Ingestion, "Queue and worker settings"):
# S3 Object Created -> EventBridge rule (input transformer -> IngestJob) -> SQS -> worker Lambda.
# Transient failures are retried by SQS (3 receives) and then moved to the DLQ; permanent ones
# are sent to the DLQ by the worker itself.

locals {
  p              = var.name_prefix
  worker_name    = "${var.name_prefix}-worker"
  worker_log     = "/aws/lambda/${var.name_prefix}-worker"
  worker_log_arn = "arn:aws:logs:${var.region}:${var.account_id}:log-group:${local.worker_log}"
}

# --- Queues --------------------------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.p}-ingest-dlq"
  message_retention_seconds = 14 * 24 * 3600 # 14 days to inspect and redrive
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "ingest" {
  name = "${local.p}-ingest"
  # At least 6x the worker timeout (AWS guidance), so a message isn't redelivered to a
  # second worker while the first is still processing it.
  visibility_timeout_seconds = 1800
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3 # transient errors only; permanent ones go to the DLQ at once
  })
}

# Only the ingest queue may use this DLQ as its dead-letter target.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.ingest.arn]
  })
}

# --- S3 event -> IngestJob -----------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "object_created" {
  name        = "${local.p}-raw-object-created"
  description = "New objects under raw/ in the raw bucket become ingest jobs."
  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [var.raw_bucket_name] }
      object = { key = [{ prefix = "raw/" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "ingest_queue" {
  rule = aws_cloudwatch_event_rule.object_created.name
  arn  = aws_sqs_queue.ingest.arn

  # Exactly the SPEC's transformer: the message carries bucket and key only. The worker
  # derives tenant, document and version from the key and checks it against the database.
  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
      size   = "$.detail.object.size"
      etag   = "$.detail.object.etag"
      time   = "$.time"
      id     = "$.id"
    }
    input_template = <<-EOT
      {"schema_version":1,"job_id":"<id>","job_type":"ingest","source":"s3_event","bucket":"<bucket>","key":"<key>","size_bytes":<size>,"etag":"<etag>","emitted_at":"<time>"}
    EOT
  }
}

data "aws_iam_policy_document" "ingest_queue" {
  statement {
    sid       = "EventBridgeRuleOnly"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingest.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    # Only this rule (confused-deputy protection): no other rule, in any account, can write here.
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.object_created.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "ingest" {
  queue_url = aws_sqs_queue.ingest.id
  policy    = data.aws_iam_policy_document.ingest_queue.json
}

# --- Image repository ----------------------------------------------------------------------

resource "aws_ecr_repository" "worker" {
  name                 = local.worker_name
  image_tag_mutability = "IMMUTABLE" # a tag always means the same image (deploys use the git SHA)

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name
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

# --- Worker role ---------------------------------------------------------------------------

data "aws_iam_policy_document" "worker_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name                 = local.worker_name
  path                 = var.workload_role_path
  assume_role_policy   = data.aws_iam_policy_document.worker_trust.json
  permissions_boundary = var.workload_boundary_arn # required by the deploy role's policy
}

# Exactly what the worker does, on exactly these resources. The boundary caps it further.
data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.worker_log_arn}:*"]
  }

  statement {
    sid = "ConsumeIngestQueue"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.ingest.arn]
  }

  statement {
    sid       = "SendPermanentFailuresToDlq"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }

  statement {
    sid       = "ReadRawFiles"
    actions   = ["s3:GetObject"]
    resources = ["${var.raw_bucket_arn}/raw/*"]
  }

  statement {
    sid       = "ReadDatabaseUrl"
    actions   = ["ssm:GetParameter"]
    resources = [var.database_url_param_arn]
  }

  statement {
    sid       = "ReadTracingKey"
    actions   = ["ssm:GetParameter"]
    resources = [var.honeycomb_api_key_param_arn]
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
    sid       = "EmbedWithOneModel"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# --- Worker Lambda -------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "worker" {
  name              = local.worker_log
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "worker" {
  function_name = local.worker_name
  role          = aws_iam_role.worker.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}"
  architectures = ["x86_64"] # images are built for linux/amd64

  timeout     = 300  # large PDFs plus embedding retries
  memory_size = 2048 # parsing is CPU-bound; Lambda CPU scales with memory
  # No reserved concurrency (DECISIONS, step 8): the account limit of 10 allows none. The
  # event source mapping's maximum_concurrency = 2 caps the worker instead.

  # Names and ids only. The DB URL and the Honeycomb key are read from SSM at cold start,
  # never put here.
  environment {
    variables = {
      RAW_BUCKET              = var.raw_bucket_name
      DLQ_URL                 = aws_sqs_queue.dlq.id
      EMBEDDING_MODEL_ID      = var.embedding_model_id
      DATABASE_URL_PARAM      = var.database_url_param_name
      HONEYCOMB_API_KEY_PARAM = var.honeycomb_api_key_param_name
      HONEYCOMB_ENDPOINT      = var.honeycomb_endpoint
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.worker.name
  }

  depends_on = [aws_iam_role_policy.worker]
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.ingest.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1 # one document per invocation isolates failures

  # At most 2 workers at once: protects Neon connections and Bedrock rate limits. Unlike a
  # reserved-concurrency cap, this stops polling instead of throttling, so a message waiting
  # its turn doesn't use up its 3 receives and land in the DLQ unprocessed.
  scaling_config {
    maximum_concurrency = 2
  }
}
