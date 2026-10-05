# -----------------------------------------------------------------------
# Scaling PR C: one more dashboard, <prefix>-<env>-edge, in the style of the pipeline and Lambda
# runs ones in main.tf -- open on a span long enough to show something (7 days, hourly points),
# counts on the left axis, a text header saying what each part is and why a widget can be
# legitimately empty. Two halves:
#
#   API Gateway   both REST APIs: requests, 4XX/5XX, latency vs integration latency, a per-status
#                 split and 429s from the access logs, and the public API's CDN next to what still
#                 reached API Gateway.
#   Firewall      every web ACL: allowed/blocked/counted over time and per rule, plus the top
#                 blocked rules, addresses and paths from the WAF logs.
#
# One dashboard, not two, and only where var.edge_dashboard_enabled (production): CloudWatch bills
# US$3 a month for every dashboard past the account's first three, and dev's edge traffic is mostly
# the operator's own.
#
# Regions: API Gateway and the regional ACLs are in the home region (var.aws_region). CloudFront's metrics, and the
# CLOUDFRONT-scope ACL's metrics and log group, only exist in us-east-1 -- a widget pointed anywhere
# else draws nothing. CloudFront metrics carry Region = "Global"; a CloudFront ACL's WAF metrics carry
# no Region at all (AWS: "Region ... required for all protected resource types except CloudFront"),
# so its widgets search both shapes rather than guess one.
#
# REST APIs publish no metric per status code, only 4XXError and 5XXError, so the 400/403/429/500/
# 502/504 split comes from the access log (Logs Insights widgets). Logs Insights is billed per GB
# scanned when the dashboard is opened; these logs are a few hundred bytes a request. A metric filter
# per status would cost a custom metric each, every month, whether anyone looks or not.
# -----------------------------------------------------------------------

locals {
  api_region = var.aws_region

  # Written out, and not the home region: CloudFront is a global service that publishes its
  # metrics only in us-east-1, and a CLOUDFRONT-scope web ACL (with its metrics and its log
  # group) can only be created there. This holds whichever region the deployment calls home.
  cloudfront_region = "us-east-1"

  api_sections = flatten([
    for api in var.api_dashboard_apis : [
      {
        type   = "text"
        width  = 24
        height = 1
        properties = {
          markdown = "### ${api.label} -- `${api.api_name}`, stage `${api.stage}`"
        }
      },
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          title  = "${api.label}: requests and errors"
          region = local.api_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiName", api.api_name, "Stage", api.stage, { stat = "Sum", label = "requests" }],
            ["AWS/ApiGateway", "4XXError", "ApiName", api.api_name, "Stage", api.stage, { stat = "Sum", label = "4XX" }],
            ["AWS/ApiGateway", "5XXError", "ApiName", api.api_name, "Stage", api.stage, { stat = "Sum", label = "5XX" }],
          ]
        }
      },
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          title  = "${api.label}: latency vs integration latency (ms)"
          region = local.api_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "ms" } }
          metrics = concat(
            [for p in ["p50", "p90", "p99"] :
            ["AWS/ApiGateway", "Latency", "ApiName", api.api_name, "Stage", api.stage, { stat = p, label = "latency ${p}" }]],
            [for p in ["p50", "p90", "p99"] :
            ["AWS/ApiGateway", "IntegrationLatency", "ApiName", api.api_name, "Stage", api.stage, { stat = p, label = "integration ${p}" }]],
          )
        }
      },
      {
        type   = "log"
        width  = 8
        height = 6
        properties = {
          title  = "${api.label}: throttled (429) per hour"
          region = local.api_region
          view   = "timeSeries"
          query  = "SOURCE '${api.access_log_group}' | filter status = 429 | stats count(*) as throttled by bin(1h)"
        }
      },
      {
        type   = "log"
        width  = 8
        height = 6
        properties = {
          title  = "${api.label}: responses by status"
          region = local.api_region
          view   = "table"
          query  = "SOURCE '${api.access_log_group}' | stats count(*) as requests by status | sort status asc"
        }
      },
      {
        type   = "log"
        width  = 16
        height = 6
        properties = {
          title  = "${api.label}: 4XX/5XX by route (errorType says who answered: THROTTLED, WAF_FILTERED, ...)"
          region = local.api_region
          view   = "table"
          query = join(" | ", [
            "SOURCE '${api.access_log_group}'",
            "filter status >= 400",
            "stats count(*) as requests by status, httpMethod, resourcePath, errorType",
            "sort requests desc",
            "limit 25",
          ])
        }
      },
    ]
  ])

  cdn_section = flatten([for cdn in(var.api_cdn == null ? [] : [var.api_cdn]) : concat(
    [
      {
        type   = "text"
        width  = 24
        height = 2
        properties = {
          markdown = "### Public API cache (CloudFront)\nRequests at the CDN against requests that still reached API Gateway: the gap is what the cache answered. Only responses the API marks cacheable (listings, articles, RSS, stats, equipment) are ever cached; POSTs and feedback never are. CloudFront's metrics live in us-east-1."
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Public API: at the CDN vs at API Gateway"
          region = local.cloudfront_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            ["AWS/CloudFront", "Requests", "DistributionId", cdn.distribution_id, "Region", "Global", { stat = "Sum", label = "requests at the CDN", region = local.cloudfront_region }],
            ["AWS/ApiGateway", "Count", "ApiName", cdn.api_name, "Stage", cdn.stage, { stat = "Sum", label = "requests that reached API Gateway", region = local.api_region }],
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Public API CDN: error rates (%)"
          region = local.cloudfront_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "%" } }
          metrics = [
            ["AWS/CloudFront", "4xxErrorRate", "DistributionId", cdn.distribution_id, "Region", "Global", { stat = "Average", label = "4xx %" }],
            ["AWS/CloudFront", "5xxErrorRate", "DistributionId", cdn.distribution_id, "Region", "Global", { stat = "Average", label = "5xx %" }],
          ]
        }
      },
    ],
    # Only when CloudFront's (paid) additional metrics are on -- otherwise this widget would always
    # be empty, which is exactly the "dashboard shows nothing" this layout avoids.
    [for enabled in(cdn.additional_metrics_enabled ? [true] : []) :
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Public API CDN: cache hit rate (%)"
          region = local.cloudfront_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, max = 100, label = "%" } }
          metrics = [
            ["AWS/CloudFront", "CacheHitRate", "DistributionId", cdn.distribution_id, "Region", "Global", { stat = "Average", label = "cache hit %" }],
          ]
        }
      }
    ],
  )])
}

locals {
  # `for _ in (cond ? [] : [1])`, not `cond ? [] : concat(...)`: the widgets are objects of different
  # shapes, so their list is a tuple, and a conditional's two results must have the same type -- an
  # empty tuple and a 17-element one don't (plan fails with "Inconsistent conditional result
  # types"; validate can't see it). This is the same idiom the sections above use.
  api_gateway_widgets = flatten([for _ in(length(var.api_dashboard_apis) == 0 ? [] : [1]) : concat(
    [
      {
        type   = "text"
        width  = 24
        height = 2
        properties = {
          markdown = "## API Gateway (${var.environment_name})\nPer API: requests and 4XX/5XX per hour, latency (whole request) against integration latency (the Lambda's share), and from the access logs the split by status and the 429s API Gateway's own throttling sent. The firewall is further down."
        }
      },
      {
        type   = "metric"
        width  = 24
        height = 4
        properties = {
          title                = "In the selected range"
          region               = local.api_region
          view                 = "singleValue"
          setPeriodToTimeRange = true
          # concat(...), not flatten: flatten goes all the way down, through each metric's own
          # list, and CloudWatch rejects the result ("metrics/0 Should be array") -- at apply, since
          # plan only sees a string.
          metrics = concat([
            for api in var.api_dashboard_apis : [
              ["AWS/ApiGateway", "Count", "ApiName", api.api_name, "Stage", api.stage, { stat = "Sum", label = "${api.label} requests" }],
              ["AWS/ApiGateway", "4XXError", "ApiName", api.api_name, "Stage", api.stage, { stat = "Sum", label = "${api.label} 4XX" }],
              ["AWS/ApiGateway", "5XXError", "ApiName", api.api_name, "Stage", api.stage, { stat = "Sum", label = "${api.label} 5XX" }],
            ]
          ]...)
        }
      },
    ],
    local.api_sections,
    local.cdn_section,
  )])
}

locals {
  # Every rule's blocks (or counts) on one ACL, found by search so a rule added later shows up by
  # itself. Rule="ALL" (the ACL's total) is left out so it doesn't draw over its own parts.
  waf_regional_sections = flatten([
    for acl in var.waf_regional_acls : [
      {
        type   = "text"
        width  = 24
        height = 1
        properties = {
          markdown = "### ${acl.label} -- web ACL `${acl.metric_name}` (${local.api_region})"
        }
      },
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          title  = "${acl.label}: allowed / blocked / counted"
          region = local.api_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            for m in ["AllowedRequests", "BlockedRequests", "CountedRequests"] :
            ["AWS/WAFV2", m, "WebACL", acl.metric_name, "Rule", "ALL", "Region", local.api_region, { stat = "Sum", label = lower(trimsuffix(m, "Requests")) }]
          ]
        }
      },
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          title  = "${acl.label}: blocked per rule (incl. rate limits)"
          region = local.api_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            [{ id = "blocked", label = "", expression = "SEARCH('{AWS/WAFV2,Region,Rule,WebACL} WebACL=\"${acl.metric_name}\" MetricName=\"BlockedRequests\" NOT Rule=\"ALL\"', 'Sum', 3600)" }],
          ]
        }
      },
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          title  = "${acl.label}: allowed and counted per rule"
          region = local.api_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            [{ id = "allowed", label = "allowed", expression = "SEARCH('{AWS/WAFV2,Region,Rule,WebACL} WebACL=\"${acl.metric_name}\" MetricName=\"AllowedRequests\" NOT Rule=\"ALL\"', 'Sum', 3600)" }],
            [{ id = "counted", label = "counted", expression = "SEARCH('{AWS/WAFV2,Region,Rule,WebACL} WebACL=\"${acl.metric_name}\" MetricName=\"CountedRequests\" NOT Rule=\"ALL\"', 'Sum', 3600)" }],
          ]
        }
      },
      {
        type   = "log"
        width  = 24
        height = 6
        properties = {
          title  = "${acl.label}: top blocked -- rule, address, path"
          region = local.api_region
          view   = "table"
          query  = local.waf_top_blocked_query[acl.log_group]
        }
      },
    ]
  ])

  # The visitor's address: x-viewer-ip when the request came through the public API's CDN (the
  # client address WAF sees then is a CloudFront edge), else the client address itself.
  waf_top_blocked_query = { for group in concat(
    [for acl in var.waf_regional_acls : acl.log_group],
    var.waf_cloudfront_acl == null ? [] : [var.waf_cloudfront_acl.log_group],
    ) : group => join(" | ", [
      "SOURCE '${group}'",
      "filter action = \"BLOCK\"",
      "parse @message /\"name\":\"x-viewer-ip\",\"value\":\"(?<viewerIp>[^\"]+)\"/",
      "fields terminatingRuleId as rule, coalesce(viewerIp, httpRequest.clientIp) as address, httpRequest.uri as path",
      "stats count(*) as blocked by rule, address, path",
      "sort blocked desc",
      "limit 25",
  ]) }

  # The CLOUDFRONT-scope ACL: us-east-1, and searched in both dimension shapes (see the header).
  waf_cloudfront_section = flatten([for cf in(var.waf_cloudfront_acl == null ? [] : [var.waf_cloudfront_acl]) : [
    {
      type   = "text"
      width  = 24
      height = 2
      properties = {
        markdown = "### ${cf.label} -- web ACL `${cf.metric_name}` (CloudFront, us-east-1)\nShared by every distribution it is attached to, so these numbers are the whole site's, not just this environment's."
      }
    },
    {
      type   = "metric"
      width  = 12
      height = 6
      properties = {
        title  = "${cf.label}: allowed / blocked / counted"
        region = local.cloudfront_region
        view   = "timeSeries"
        period = 3600
        yAxis  = { left = { min = 0, label = "per hour" } }
        metrics = [
          [{ id = "total", label = "", expression = "SEARCH('{AWS/WAFV2,Rule,WebACL} WebACL=\"${cf.metric_name}\" Rule=\"ALL\"', 'Sum', 3600)" }],
          [{ id = "total_regioned", label = "", expression = "SEARCH('{AWS/WAFV2,Region,Rule,WebACL} WebACL=\"${cf.metric_name}\" Rule=\"ALL\"', 'Sum', 3600)" }],
        ]
      }
    },
    {
      type   = "metric"
      width  = 12
      height = 6
      properties = {
        title  = "${cf.label}: blocked per rule (incl. rate limit)"
        region = local.cloudfront_region
        view   = "timeSeries"
        period = 3600
        yAxis  = { left = { min = 0, label = "per hour" } }
        metrics = [
          [{ id = "blocked", label = "", expression = "SEARCH('{AWS/WAFV2,Rule,WebACL} WebACL=\"${cf.metric_name}\" MetricName=\"BlockedRequests\" NOT Rule=\"ALL\"', 'Sum', 3600)" }],
          [{ id = "blocked_regioned", label = "", expression = "SEARCH('{AWS/WAFV2,Region,Rule,WebACL} WebACL=\"${cf.metric_name}\" MetricName=\"BlockedRequests\" NOT Rule=\"ALL\"', 'Sum', 3600)" }],
        ]
      }
    },
    {
      type   = "log"
      width  = 24
      height = 6
      properties = {
        title  = "${cf.label}: top blocked -- rule, address, path"
        region = local.cloudfront_region
        view   = "table"
        query  = local.waf_top_blocked_query[cf.log_group]
      }
    },
  ]])
}

locals {
  # The same idiom as api_gateway_widgets, for the same reason.
  waf_widgets = flatten([for _ in(length(var.waf_regional_acls) == 0 && var.waf_cloudfront_acl == null ? [] : [1]) : concat(
    [
      {
        type   = "text"
        width  = 24
        height = 2
        properties = {
          markdown = "## Firewall (${var.environment_name})\nAllowed, blocked and counted requests per web ACL and per rule, rate limits included. WAF only reports a number when it is above zero, so a rule that blocked nothing draws no line. The visitor ACLs log **blocked requests only** (privacy policy, section 5); the admin ACL logs everything."
        }
      },
    ],
    local.waf_regional_sections,
    local.waf_cloudfront_section,
  )])
}

resource "aws_cloudwatch_dashboard" "edge" {
  count          = var.edge_dashboard_enabled && length(concat(local.api_gateway_widgets, local.waf_widgets)) > 0 ? 1 : 0
  dashboard_name = "${var.unique_name_prefix}-${var.environment_name}-edge"

  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets        = concat(local.api_gateway_widgets, local.waf_widgets)
  })
}
