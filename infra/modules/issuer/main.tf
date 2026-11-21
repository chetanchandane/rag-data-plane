# Token issuer (step 11, DECISIONS 2026-11-21): the OIDC discovery document and the JWKS that
# API Gateway's JWT authorizer reads, served over HTTPS by CloudFront from a private bucket.
#
#   https://<distribution>/.well-known/openid-configuration  -> issuer, jwks_uri
#   https://<distribution>/.well-known/jwks.json              -> the public signing keys
#
# Only public keys pass through here. Private keys are in SSM, written by scripts/issuer.py.

locals {
  discovery_key = ".well-known/openid-configuration"
  jwks_key      = ".well-known/jwks.json"
  origin_id     = "issuer-bucket"

  # The issuer is the `iss` of every token: no trailing slash, so `${issuer}/.well-known/...`
  # is the discovery URL verifiers build from it.
  issuer = "https://${aws_cloudfront_distribution.issuer.domain_name}"
}

# --- Bucket --------------------------------------------------------------------------------

resource "aws_s3_bucket" "issuer" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "issuer" {
  bucket                  = aws_s3_bucket.issuer.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3; OAC reads it without any KMS key policy
    }
  }
}

# --- CloudFront ----------------------------------------------------------------------------

# CloudFront signs its requests to S3 (SigV4), so the bucket can stay private.
resource "aws_cloudfront_origin_access_control" "issuer" {
  name                              = var.bucket_name
  description                       = "CloudFront reads the token issuer's documents."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Caches for as long as the origin's Cache-Control says (min 1 s, max 1 year), so the
# objects' max-age decides it.
data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

resource "aws_cloudfront_distribution" "issuer" {
  enabled         = true
  comment         = "Token issuer: OIDC discovery and JWKS"
  price_class     = "PriceClass_100" # North America and Europe edges only; the cheapest
  is_ipv6_enabled = true
  http_version    = "http2and3"

  origin {
    origin_id                = local.origin_id
    domain_name              = aws_s3_bucket.issuer.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.issuer.id
  }

  default_cache_behavior {
    target_origin_id       = local.origin_id
    viewer_protocol_policy = "https-only" # plain HTTP is refused, not redirected
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id
    compress               = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # *.cloudfront.net; a custom domain would need ACM
  }
}

# --- Bucket policy -------------------------------------------------------------------------

data "aws_iam_policy_document" "issuer" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.issuer.arn, "${aws_s3_bucket.issuer.arn}/*"]
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

  # Only this distribution, and only the two documents. Any other key is a 403 at the edge.
  statement {
    sid     = "CloudFrontReadsIssuerDocuments"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.issuer.arn}/${local.discovery_key}",
      "${aws_s3_bucket.issuer.arn}/${local.jwks_key}",
    ]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.issuer.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "issuer" {
  bucket     = aws_s3_bucket.issuer.id
  policy     = data.aws_iam_policy_document.issuer.json
  depends_on = [aws_s3_bucket_public_access_block.issuer]
}

# --- Documents -----------------------------------------------------------------------------

resource "aws_s3_object" "discovery" {
  bucket        = aws_s3_bucket.issuer.id
  key           = local.discovery_key
  content_type  = "application/json"
  cache_control = "public, max-age=${var.cache_max_age_seconds}"
  # Partial on purpose: there is no authorization endpoint, and verifiers need only these.
  content = jsonencode({
    issuer   = local.issuer
    jwks_uri = "${local.issuer}/${local.jwks_key}"
  })
}

resource "aws_s3_object" "jwks" {
  bucket        = aws_s3_bucket.issuer.id
  key           = local.jwks_key
  content_type  = "application/json"
  cache_control = "public, max-age=${var.cache_max_age_seconds}"
  content       = var.jwks_json
}
