variable "force_destroy" {
  type        = bool
  default     = true
  description = "Whether the site S3 bucket can be destroyed even when non-empty. Dev defaults to true so `terraform destroy` never chokes on a non-empty bucket -- dev is meant to be torn down and rebuilt freely."
}

variable "web_acl_arn" {
  type        = string
  default     = ""
  description = <<-EOT
    ARN of the shared WAFv2 Web ACL created in
    infra/environments/production (CLOUDFRONT scope, us-east-1). Dev does
    not own WAF creation -- one ACL is shared across both environments
    rather than creating one each. Leave empty until production's WAF ACL
    has been applied at least once; then set this in terraform.tfvars so
    dev's distribution shares the same ACL as production. This is a manual
    follow-up step, not automated.
  EOT
}
