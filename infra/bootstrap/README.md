# infra/bootstrap

Applied once, by hand, from Chetan's laptop. CI can't create these resources, because CI uses them to deploy:

| Resource | Why |
| --- | --- |
| `ragdp-tfstate-905418224080` (S3) | Terraform state for `infra/envs/prod`, with S3 native locking. Versioned, encrypted, private, TLS only. |
| GitHub OIDC provider | CI gets short-lived credentials from GitHub's OIDC token. There are no access keys anywhere. |
| `ragdp-github-deploy` (IAM role) | Assumable only from `chetanchandane/rag-data-plane`, environment `prod`. Can manage `ragdp-*` resources in this account and Region. |
| `ragdp-workload-boundary` (IAM policy) | Permissions boundary. Every role the deploy role creates must carry it, so the deploy role can't create a role more powerful than itself. |

The provider refuses to run against any account except `905418224080`.

## Apply (first time)

```bash
aws sts get-caller-identity --profile ragdp      # must show 905418224080
cd infra/bootstrap
terraform init
AWS_PROFILE=ragdp terraform plan -out=bootstrap.tfplan
AWS_PROFILE=ragdp terraform apply bootstrap.tfplan
```

## Move this stack's state into the bucket (once, after the first apply)

The state starts local because the bucket doesn't exist before the first apply. Afterwards, create `backend.tf` here with the following content:

```hcl
terraform {
  backend "s3" {
    bucket       = "ragdp-tfstate-905418224080"
    key          = "bootstrap/terraform.tfstate"
    region       = "us-east-2"
    use_lockfile = true
    encrypt      = true
  }
}
```

Then run `AWS_PROFILE=ragdp terraform init -migrate-state`, answer `yes`, and delete the local `terraform.tfstate*` files. The deploy role can't read `bootstrap/`; only the `envs/prod/` state is reachable from CI.

## Changing it later

Slice 2 adds API Gateway permissions to the deploy role. Any change to the deploy role or the boundary is a bootstrap change, applied from the laptop the same way. CI can't change its own permissions.
