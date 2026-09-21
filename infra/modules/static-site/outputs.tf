output "distribution_domain_name" {
  value       = aws_cloudfront_distribution.site.domain_name
  description = "The distribution's default *.cloudfront.net domain name (always populated, even when a custom domain is also configured)."
}

output "distribution_id" {
  value       = aws_cloudfront_distribution.site.id
  description = "CloudFront distribution ID."
}

output "bucket_name" {
  value       = aws_s3_bucket.site.bucket
  description = "Name of the site content S3 bucket."
}

output "www_redirect_enabled" {
  value       = local.www_redirect
  description = "Whether www.<domain_name> is also served and redirected to the bare domain."
}

output "custom_domain_url" {
  value       = var.enable_custom_domain ? "https://${var.domain_name}" : null
  description = "The site's custom domain URL, when enable_custom_domain = true; null otherwise."
}
