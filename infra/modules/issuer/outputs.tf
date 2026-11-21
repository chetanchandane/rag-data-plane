output "issuer_url" {
  description = "The tokens' `iss` and the JWT authorizer's issuer (no trailing slash)."
  value       = local.issuer
}

output "distribution_id" {
  description = "For a cache invalidation, if a key change can't wait for max-age."
  value       = aws_cloudfront_distribution.issuer.id
}
