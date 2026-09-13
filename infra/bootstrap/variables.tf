variable "aws_region" {
  type        = string
  default     = "ap-southeast-2"
  description = "Region for the bootstrap resources (state bucket, OIDC provider, IAM roles). Bootstrap is applied once, locally, by a human -- never through CI."
}

variable "state_bucket_name" {
  type        = string
  default     = "bloggerbear-terraform-state"
  description = <<-EOT
    Name of the S3 bucket used as the Terraform state backend for every
    environment in this project. S3 bucket names are globally unique across
    ALL AWS accounts, not just this one -- confirm this exact name is
    actually available (e.g. `aws s3api head-bucket --bucket
    bloggerbear-terraform-state`, expecting a 404/NoSuchBucket error) BEFORE
    running the one-time bootstrap apply.

    If it collides, change this default to something unique (e.g. append
    your account ID or a random suffix) -- but you MUST then copy-paste
    that exact literal string into the `backend "s3" { bucket = "..." }`
    block in BOTH infra/environments/dev/main.tf and
    infra/environments/production/main.tf, since Terraform backend blocks
    cannot reference variables or interpolation of any kind.
  EOT
}

variable "github_repo" {
  type        = string
  default     = "AllainWoodsford/BloggerBear"
  description = "GitHub <owner>/<repo> slug allowed to assume the deploy roles via OIDC."
}
