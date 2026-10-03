output "url" {
  value       = "https://${aws_cloudfront_distribution.api.domain_name}"
  description = "Base URL of the public API through the CDN (no stage in the path) -- the frontend's PUBLIC_API_URL."
}

output "domain_name" {
  value       = aws_cloudfront_distribution.api.domain_name
  description = "The distribution's *.cloudfront.net hostname, for the site's Content-Security-Policy connect-src."
}

output "distribution_id" {
  value       = aws_cloudfront_distribution.api.id
  description = "CloudFront distribution ID -- the DistributionId dimension of its metrics."
}

output "additional_metrics_enabled" {
  value       = var.enable_additional_metrics
  description = "Whether CacheHitRate and the other additional metrics are being published."
}
