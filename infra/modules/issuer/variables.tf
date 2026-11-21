variable "bucket_name" {
  description = "Private bucket holding the discovery document and the JWKS."
  type        = string
}

variable "jwks_json" {
  description = "The public JWKS (infra/envs/prod/jwks.json, written by scripts/issuer.py)."
  type        = string

  # Only public RSA signing keys with a kid: a private member (d, p, q, ...) here would
  # publish a private key to the internet.
  validation {
    condition = can(jsondecode(var.jwks_json).keys) && length(jsondecode(var.jwks_json).keys) > 0 && alltrue([
      for k in jsondecode(var.jwks_json).keys :
      try(k.kty == "RSA" && k.alg == "RS256" && k.use == "sig" && length(k.kid) > 0, false)
      && length(setintersection(keys(k), ["d", "p", "q", "dp", "dq", "qi"])) == 0
    ])
    error_message = "jwks_json must hold at least one public RSA signing key (kty RSA, alg RS256, use sig, a kid) and no private members."
  }
}

variable "cache_max_age_seconds" {
  description = "Cache-Control max-age on both documents: how long a key change takes to reach CloudFront and clients."
  type        = number
  default     = 300
}
