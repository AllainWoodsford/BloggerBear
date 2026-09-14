terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
      # ACM certificates for CloudFront must be requested/validated in
      # us-east-1 regardless of hosting region (AWS platform requirement).
      # This module never declares its own us-east-1 provider block --
      # that would hardcode a region choice inside a reusable module. The
      # calling environment declares the real provider and passes it in
      # via `providers = { aws.us_east_1 = aws.us_east_1 }`.
      configuration_aliases = [aws.us_east_1]
    }
  }
}

# -----------------------------------------------------------------------
# Site content bucket -- private, OAC-only access. No public bucket
# policy, no public ACLs.
#
# AVD-AWS-0132 ("no customer-managed KMS key") ignored deliberately.
# Every bucket/topic/queue in this project uses AWS's default managed-key
# encryption, not a customer-managed KMS key -- a cost/complexity
# trade-off for a single-operator portfolio project (see the same
# comment on infra/environments/dev/main.tf's aws_s3_bucket.content for
# the full rationale).
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket" "site" {
  bucket        = "bloggerbear-${var.environment_name}-site"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "site" {
  bucket = aws_s3_bucket.site.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket = aws_s3_bucket.site.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------
# CloudFront distribution with Origin Access Control (not the legacy OAI).
# -----------------------------------------------------------------------
resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "bloggerbear-${var.environment_name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# -----------------------------------------------------------------------
# Security response headers -- the real equivalent of ".htaccess security
# headers" on this stack. There is no Apache anywhere in this project
# (S3 + CloudFront), so an .htaccess file would be silently inert if one
# were deployed; a CloudFront response headers policy is what actually
# applies these headers to every response this distribution serves.
#
# CSP note: connect-src allows any *.execute-api.<region>.amazonaws.com
# host (the AWS-assigned pattern for HTTP API invoke URLs), rather than
# threading the exact, dynamically-created public API's hostname through
# as a new module input -- simpler and avoids coupling this module to
# the calling environment's API Gateway resources, while still only
# allowing AWS API Gateway in this project's one fixed region (never an
# arbitrary third-party origin). script-src/style-src/font-src stay
# 'self' with no 'unsafe-inline' -- frontend/app.js has no inline
# scripts or inline style attributes anywhere, and every stylesheet is a
# same-origin <link>, so nothing here needs loosening.
# -----------------------------------------------------------------------
resource "aws_cloudfront_response_headers_policy" "security" {
  name = "bloggerbear-${var.environment_name}-security-headers"

  security_headers_config {
    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }

    strict_transport_security {
      access_control_max_age_sec = 63072000 # 2 years
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    xss_protection {
      protection = true
      mode_block = true
      override   = true
    }

    content_security_policy {
      content_security_policy = join("; ", [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self' https://*.execute-api.ap-southeast-2.amazonaws.com",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
      ])
      override = true
    }
  }

  # Permissions-Policy has no typed block in security_headers_config (as
  # of the AWS provider versions this project pins) -- set via
  # custom_headers_config instead. Denies every sensitive browser
  # feature this site has no use for.
  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      value    = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
      override = true
    }
  }
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  default_root_object = "index.html"
  aliases             = var.enable_custom_domain ? [var.domain_name] : []
  web_acl_id          = var.web_acl_id != "" ? var.web_acl_id : null

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-${var.environment_name}-site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    target_origin_id           = "s3-${var.environment_name}-site"
    viewer_protocol_policy     = "redirect-to-https"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }
  }

  # This is a client-side-routed SPA -- everything real lives under one
  # index.html with hash-based routing (see frontend/app.js), so the
  # only way S3/CloudFront itself ever returns an error is a request for
  # a path that isn't an actual uploaded object (S3 behind OAC returns
  # 403, not 404, for a missing key -- both get mapped to the same
  # friendly static page here, at HTTP 404, rather than leaking S3's raw
  # AccessDenied/NoSuchKey XML to visitors). error_caching_min_ttl is
  # short so a since-fixed path stops erroring quickly rather than
  # staying cached as broken.
  custom_error_response {
    error_code            = 403
    response_code         = 404
    response_page_path    = "/error.html"
    error_caching_min_ttl = 60
  }

  custom_error_response {
    error_code            = 404
    response_code         = 404
    response_page_path    = "/error.html"
    error_caching_min_ttl = 60
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = var.enable_custom_domain ? null : true
    acm_certificate_arn            = var.enable_custom_domain ? aws_acm_certificate_validation.site[0].certificate_arn : null
    ssl_support_method             = var.enable_custom_domain ? "sni-only" : null
    minimum_protocol_version       = var.enable_custom_domain ? "TLSv1.2_2021" : null
  }
}

# Bucket policy granting the CloudFront distribution (and only this
# distribution, via SourceArn) read access -- this is what OAC uses in
# place of a public bucket policy.
resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontServicePrincipalReadOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.site.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.site.arn
          }
        }
      }
    ]
  })
}

# -----------------------------------------------------------------------
# Custom domain path (production only): ACM cert in us-east-1, DNS
# validation, CloudFront alias, Route 53 alias records. Entirely skipped
# when enable_custom_domain = false (dev).
# -----------------------------------------------------------------------
resource "aws_acm_certificate" "site" {
  count = var.enable_custom_domain ? 1 : 0

  provider          = aws.us_east_1
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = var.enable_custom_domain ? {
    for dvo in aws_acm_certificate.site[0].domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  zone_id         = var.hosted_zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "site" {
  count = var.enable_custom_domain ? 1 : 0

  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.site[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "site_a" {
  count = var.enable_custom_domain ? 1 : 0

  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "site_aaaa" {
  count = var.enable_custom_domain ? 1 : 0

  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}
