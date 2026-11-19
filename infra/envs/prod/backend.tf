# State lives in the bootstrap bucket. use_lockfile: S3 native locking (Terraform >= 1.10),
# so no DynamoDB table. The deploy role can reach only this key and its .tflock file.
terraform {
  backend "s3" {
    bucket       = "ragdp-tfstate-905418224080"
    key          = "envs/prod/terraform.tfstate"
    region       = "us-east-2"
    use_lockfile = true
    encrypt      = true
  }
}
