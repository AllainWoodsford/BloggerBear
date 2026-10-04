output "state_bucket_name" {
  value       = aws_s3_bucket.terraform_state.bucket
  description = <<-EOT
    Name of the Terraform state bucket. If this differs from the literal
    "bloggerbear-terraform-state" (e.g. because the default collided and
    var.state_bucket_name was changed before applying), set this exact
    value as the GitHub Actions variable TF_STATE_BUCKET_DEV and/or
    TF_STATE_BUCKET_PROD -- backend blocks cannot reference variables, so
    CI passes it to `terraform init -backend-config="bucket=..."` instead.
    See docs/deploying-your-own.md.
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

output "hosted_zone_id" {
  value       = one(aws_route53_zone.site[*].zone_id)
  description = "Route 53 hosted zone ID for var.domain_name (null when no domain is set). Set this as `hosted_zone_id` in infra/environments/production/terraform.tfvars."
}

output "hosted_zone_name_servers" {
  value       = one(aws_route53_zone.site[*].name_servers)
  description = "The four name servers to enter as CUSTOM nameservers at your registrar (GoDaddy: Domain Settings > Nameservers > Change > Enter my own). Null when no domain is set."
}
