# -----------------------------------------------------------------------
# Scaling PR C: a CloudFront distribution in front of the public API, so the listing, article and
# RSS responses every visitor asks for are served from the edge instead of invoking the Lambda (and
# reading DynamoDB) per request.
#
# Its own distribution rather than an /api/* behaviour on the site's: the site distribution maps
# every 403 and 404 to /error.html (custom_error_response is distribution-wide, never per
# behaviour), which would replace the API's JSON errors with an HTML page -- and the feedback form
# reads the body of a 403 to retry its verification (frontend/verify.js). A second distribution has
# no monthly charge; only its requests are billed.
#
# What gets cached is decided by the API itself: the cache policy's default TTL is 0, so a response
# is cached only when public_api_handler.py sends Cache-Control with a max-age (listings, articles,
# RSS, stats). Everything else -- the view counter, feedback, feedback-status (it hands out a fresh
# verification token), errors -- is sent no-store and passes straight through. POSTs are never
# cached by CloudFront at all.
#
# API Gateway's own cache was the alternative: it bills per hour whether used or not (the smallest,
# 0.5 GB, is about US$0.028/hour in Sydney, ~US$20 a month per stage), more than this whole site's
# monthly bill. Not used.
# -----------------------------------------------------------------------

# Puts the visitor's own address on every request, for the regional WAF's per-visitor rate limits:
# behind CloudFront the API only ever sees edge addresses, which many visitors share. Set from
# CloudFront's own record of the connection, overwriting anything the visitor sent, so it cannot be
# forged through the CDN. (Direct calls to the API can forge it, which is why the WAF only trusts it
# on requests that also carry the origin_verify header below.)
resource "aws_cloudfront_function" "viewer_ip" {
  name    = "${var.unique_name_prefix}-${var.environment_name}-api-viewer-ip"
  runtime = "cloudfront-js-2.0"
  comment = "Adds the viewer IP for the public API's WAF rate limits"
  publish = true
  code    = file("${path.module}/viewer_ip.js")
}

resource "aws_cloudfront_cache_policy" "api" {
  name        = "${var.unique_name_prefix}-${var.environment_name}-public-api"
  comment     = "Cache only what the public API marks cacheable (default TTL 0)"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true

    # GET /articles?topic_id=...&page=... and GET /musings?page=... read query strings; all of them
    # are in the key so a new one can never be served another's answer.
    query_strings_config {
      query_string_behavior = "all"
    }

    headers_config {
      header_behavior = "none"
    }

    cookies_config {
      cookie_behavior = "none"
    }
  }
}

# AWS's managed "everything the viewer sent, except Host" policy -- API Gateway rejects a Host that
# isn't its own. Forwarded headers are not part of the cache key (the cache policy above decides
# that), so the viewer-IP header does not split the cache per visitor.
data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "api" {
  # checkov:skip=CKV_AWS_68:the web ACL comes in as var.web_acl_id (production attaches the shared CloudFront ACL); the public API's regional WAF guards the origin either way
  # checkov:skip=CKV_AWS_174:the default *.cloudfront.net certificate cannot set a minimum TLS version; the origin itself is TLS 1.2 only
  # checkov:skip=CKV2_AWS_42:the API is reached at its *.cloudfront.net name; only the site takes the custom domain
  # checkov:skip=CKV_AWS_305:an API, not a site: there is no root object to serve
  # checkov:skip=CKV2_AWS_32:JSON responses read by app.js, never rendered as a page; the site's distribution sets the security headers (CSP, HSTS)
  enabled         = true
  comment         = "${var.unique_name_prefix}-${var.environment_name} public API"
  is_ipv6_enabled = true
  web_acl_id      = var.web_acl_id != "" ? var.web_acl_id : null

  origin {
    domain_name = var.api_domain
    origin_id   = "public-api"
    origin_path = "/${var.stage_name}"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    # Tells the regional WAF a request came through this distribution (see viewer_ip above). Not a
    # credential for anything else; the WAF log configuration redacts it.
    custom_header {
      name  = "x-origin-verify"
      value = var.origin_verify_secret
    }
  }

  default_cache_behavior {
    target_origin_id         = "public-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = aws_cloudfront_cache_policy.api.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    compress                 = true

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.viewer_ip.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# CloudFront's additional metrics (CacheHitRate, OriginLatency, error rates by status) cost extra --
# they bill like CloudWatch custom metrics, roughly US$2.40 a month for one distribution -- so they
# are off unless asked for. Without them the API Gateway dashboard still shows the cache working:
# requests at the CDN next to requests that reached API Gateway.
resource "aws_cloudfront_monitoring_subscription" "api" {
  count = var.enable_additional_metrics ? 1 : 0

  distribution_id = aws_cloudfront_distribution.api.id

  monitoring_subscription {
    realtime_metrics_subscription_config {
      realtime_metrics_subscription_status = "Enabled"
    }
  }
}
