# Added after the first apply created the bucket (see README.md, "Move this stack's state").
# The deploy role can't reach this key: only envs/prod/ is in its policy.
terraform {
  backend "s3" {
    bucket       = "ragdp-tfstate-905418224080"
    key          = "bootstrap/terraform.tfstate"
    region       = "us-east-2"
    use_lockfile = true
    encrypt      = true
  }
}
