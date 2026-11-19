# Raw bucket (SPEC: Infrastructure, "storage"): source PDFs at
# raw/{tenant_id}/{doc_id}/v{version}/source.pdf, uploaded by clients with presigned PUTs.

resource "aws_s3_bucket" "raw" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3
    }
  }
}

# Every new object becomes an "Object Created" event on the default EventBridge bus; the
# ingest module's rule turns it into an IngestJob.
resource "aws_s3_bucket_notification" "raw" {
  bucket      = aws_s3_bucket.raw.id
  eventbridge = true
}

resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Browsers need CORS to PUT to a presigned URL. Without any allowed origin there is no CORS
# rule, and only non-browser clients (curl, servers) can upload.
resource "aws_s3_bucket_cors_configuration" "raw" {
  count  = length(var.upload_cors_origins) > 0 ? 1 : 0
  bucket = aws_s3_bucket.raw.id

  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = var.upload_cors_origins
    allowed_headers = ["content-type", "x-amz-checksum-sha256", "x-amz-sdk-checksum-algorithm"]
    max_age_seconds = 3000
  }
}

data "aws_iam_policy_document" "raw" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.raw.arn, "${aws_s3_bucket.raw.arn}/*"]
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

resource "aws_s3_bucket_policy" "raw" {
  bucket     = aws_s3_bucket.raw.id
  policy     = data.aws_iam_policy_document.raw.json
  depends_on = [aws_s3_bucket_public_access_block.raw]
}
