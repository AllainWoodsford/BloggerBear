output "state_bucket_name" {
  value       = aws_s3_bucket.terraform_state.bucket
  description = <<-EOT
    Name of the Terraform state bucket. If this differs from the literal
    "bloggerbear-terraform-state" (e.g. because the default collided and
    var.state_bucket_name was changed before applying), copy this exact
    value into the `backend "s3" { bucket = "..." }` block in BOTH
    infra/environments/dev/main.tf and infra/environments/production/main.tf
    -- backend blocks cannot reference variables or interpolation of any
    kind, so the literal string must match exactly in all three places.
  EOT
}

output "oidc_provider_arn" {
  value       = aws_iam_openid_connect_provider.github_actions.arn
  description = "ARN of the GitHub Actions OIDC identity provider."
}

output "dev_deploy_role_arn" {
  value       = aws_iam_role.gha_dev_deploy.arn
  description = "Set this as the `role-to-assume` GitHub Environment variable on the `dev` Environment."
}

output "prod_deploy_role_arn" {
  value       = aws_iam_role.gha_prod_deploy.arn
  description = "Set this as the `role-to-assume` GitHub Environment variable on the `production` Environment."
}
