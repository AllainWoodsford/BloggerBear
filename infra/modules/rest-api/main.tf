# -----------------------------------------------------------------------
# Reusable Lambda-proxied REST API (API Gateway v1) module.
#
# Deliberately REST API (v1), not the simpler/cheaper HTTP API (v2) this
# project's admin/public APIs originally used -- AWS WAFv2 cannot
# associate with API Gateway HTTP APIs at all (only REST APIs, ALB,
# AppSync, Cognito user pools, App Runner, and Verified Access), confirmed
# the hard way when the first real apply's aws_wafv2_web_acl_association
# calls failed with "The ARN isn't valid" against an apigatewayv2 stage
# ARN -- not a permissions problem, a fundamentally unsupported resource
# type. This module exists to make that migration as close to a drop-in
# replacement as REST API's more verbose (parent/child resource tree,
# rather than a flat route list) resource model allows.
#
# One instance of this module = one API with one stage, proxying every
# route to the same Lambda function -- this project's admin/public APIs
# are each backed by exactly one Lambda handler with its own internal
# route dispatch (event["routeKey"], or the http_method+resource fallback
# this migration added -- see lambdas/admin_api_handler.py and
# public_api_handler.py).
# -----------------------------------------------------------------------

resource "aws_api_gateway_rest_api" "this" {
  name = var.name

  # Regional, not the default edge-optimized -- matches the HTTP API
  # predecessor (which was always regional) and is required for a
  # REGIONAL-scope WAFv2 Web ACL to associate with this API's stage.
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

locals {
  routes = { for r in var.routes : r => {
    method = split(" ", r)[0]
    path   = split(" ", r)[1]
  } }

  route_paths = toset([for r in local.routes : r.path])

  # See var.enable_cors's own description for why this exists at all.
  cors_routes = var.enable_cors ? { for p in local.route_paths : "OPTIONS ${p}" => {
    method = "OPTIONS"
    path   = p
  } } : {}

  all_routes = merge(local.routes, local.cors_routes)

  # Every path segment that needs its own aws_api_gateway_resource node,
  # including intermediate parents with no method of their own (e.g.
  # "/moderation-queue/{queue_id}" needs a node even though nothing routes
  # to exactly that path -- only its /approve and /reject children do).
  # split("/", "/a/b") => ["", "a", "b"], so the useful prefixes run from
  # index 1 (just "/a") through the full path.
  all_paths = toset(flatten([
    for r in local.all_routes : [
      for i in range(1, length(split("/", r.path))) :
      join("/", slice(split("/", r.path), 0, i + 1))
    ]
  ]))

  # Parent path for each node -- "" means its parent is the API's own
  # root resource, not another node this module creates.
  parent_of = { for p in local.all_paths : p =>
    length(split("/", p)) <= 2 ? "" : join("/", slice(split("/", p), 0, length(split("/", p)) - 1))
  }

  # The single new path segment each node's path_part represents. API
  # Gateway wants the literal "{param}" syntax here for a path-parameter
  # segment, same as it already appears in the path itself.
  segment_of = { for p in local.all_paths : p => split("/", p)[length(split("/", p)) - 1] }

  # Number of path segments each node has (1 for "/topics", 2 for
  # "/topics/{id}", etc.) -- see the level1-4 resource blocks below for
  # why this matters.
  depth_of = { for p in local.all_paths : p => length(split("/", p)) - 1 }

  paths_by_depth = { for d in range(1, 5) : d => toset([
    for p in local.all_paths : p if local.depth_of[p] == d
  ]) }

  # Unified path -> resource-id lookup across all four level resources
  # below, for aws_api_gateway_method/_integration to use without caring
  # which level actually created a given path's resource.
  resource_id_of = merge(
    { for k, r in aws_api_gateway_resource.level1 : k => r.id },
    { for k, r in aws_api_gateway_resource.level2 : k => r.id },
    { for k, r in aws_api_gateway_resource.level3 : k => r.id },
    { for k, r in aws_api_gateway_resource.level4 : k => r.id },
  )
}

# Split into one resource block per tree depth (max 4 -- this project's
# deepest route is admin's 4-segment
# /prompt-refinements/{topic_id}/{version}/approve) rather than a single
# self-referential for_each, because Terraform can't build a valid
# dependency graph for one for_each instance referencing ANOTHER instance
# of the SAME resource via a dynamically-computed key (parent_id looking
# up aws_api_gateway_resource.this[parent_of[each.value]] from within
# aws_api_gateway_resource.this itself) -- confirmed the hard way via
# `terraform plan`, which refused with "Error: Cycle" naming every
# instance of the resource. Each level here is a DIFFERENT resource block
# (level2 depends on level1, level3 on level2, level4 on level3), which
# Terraform can order correctly since the dependency is between distinct
# resource types, not within one. Adding a route deeper than 4 segments
# needs a level5 block added the same way.
resource "aws_api_gateway_resource" "level1" {
  for_each = local.paths_by_depth[1]

  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = local.segment_of[each.value]
}

resource "aws_api_gateway_resource" "level2" {
  for_each = local.paths_by_depth[2]

  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.level1[local.parent_of[each.value]].id
  path_part   = local.segment_of[each.value]
}

resource "aws_api_gateway_resource" "level3" {
  for_each = local.paths_by_depth[3]

  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.level2[local.parent_of[each.value]].id
  path_part   = local.segment_of[each.value]
}

resource "aws_api_gateway_resource" "level4" {
  for_each = local.paths_by_depth[4]

  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.level3[local.parent_of[each.value]].id
  path_part   = local.segment_of[each.value]
}

resource "aws_api_gateway_method" "this" {
  for_each = local.all_routes

  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = local.resource_id_of[each.value.path]
  http_method   = each.value.method
  authorization = var.authorization
}

resource "aws_api_gateway_integration" "this" {
  for_each = local.all_routes

  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = local.resource_id_of[each.value.path]
  http_method = aws_api_gateway_method.this[each.key].http_method
  # AWS_PROXY integrations always call Lambda via POST internally,
  # regardless of the method the client actually used -- not a bug.
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = var.lambda_invoke_arn
}

# API Gateway v1 deployments are immutable snapshots -- redeploying on any
# resource/method/integration change requires a brand new
# aws_api_gateway_deployment. Terraform has no built-in "redeploy when any
# of these change", so this hashes every one of their ids into the
# deployment's own trigger to force that.
resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeployment = sha1(jsonencode({
      resources    = local.resource_id_of
      methods      = { for k, m in aws_api_gateway_method.this : k => m.id }
      integrations = { for k, i in aws_api_gateway_integration.this : k => i.id }
    }))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "this" {
  # checkov:skip=CKV2_AWS_4:execution logging is off on purpose (see aws_api_gateway_method_settings below); the access log records every request
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = var.stage_name

  # Scaling PR C: one JSON line per request, for the API Gateway dashboard's per-status split
  # (REST APIs publish only 4XXError/5XXError as metrics, never 400 vs 403 vs 429). No IP address,
  # user agent or other visitor detail -- only what happened to the request -- so this is not a
  # log of who browsed what. Written by hand rather than with jsonencode() so status and latency
  # are JSON numbers Logs Insights can compare; integrationLatency stays quoted because API Gateway
  # writes "-" for a request that never reached the Lambda (blocked, throttled).
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = join("", [
      "{",
      "\"requestId\":\"$context.requestId\",",
      "\"requestTime\":\"$context.requestTime\",",
      "\"httpMethod\":\"$context.httpMethod\",",
      "\"resourcePath\":\"$context.resourcePath\",",
      "\"status\":$context.status,",
      "\"responseLatency\":$context.responseLatency,",
      "\"integrationLatency\":\"$context.integrationLatency\",",
      "\"responseLength\":\"$context.responseLength\",",
      "\"errorType\":\"$context.error.responseType\",",
      "\"wafStatus\":\"$context.wafResponseCode\"",
      "}",
    ])
  }
}

# Access logs for the stage above. API Gateway writes them through the account-wide CloudWatch Logs
# role that infra/bootstrap sets (aws_api_gateway_account) -- without it the stage update fails with
# "CloudWatch Logs role ARN must be set in account settings".
resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${var.name}-access"
  retention_in_days = var.access_log_retention_days
}

# Scaling PR C: throttling. Every method in the stage gets the default rate (steady requests per
# second) and burst (how many may arrive at once); API Gateway answers anything beyond them with a
# 429 before the Lambda is invoked, so a flood costs neither Lambda time nor DynamoDB reads. This is
# a whole-API ceiling, not a per-visitor one -- per-IP limits are WAF's job. Detailed per-method
# CloudWatch metrics stay off (they bill as custom metrics), as does execution logging.
resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.this.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = var.throttling_rate_limit
    throttling_burst_limit = var.throttling_burst_limit
    metrics_enabled        = false
    logging_level          = "OFF"
  }
}

# Tighter limits for individual routes (e.g. the feedback POST, which can cost a model call). API
# Gateway names a method by its resource path with every "/" written as "~1", then the HTTP method:
# "POST /articles/{article_id}/feedback" -> "~1articles~1{article_id}~1feedback/POST".
resource "aws_api_gateway_method_settings" "route" {
  for_each = var.method_throttling

  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.this.stage_name
  method_path = "${replace(split(" ", each.key)[1], "/", "~1")}/${split(" ", each.key)[0]}"

  settings {
    throttling_rate_limit  = each.value.rate_limit
    throttling_burst_limit = each.value.burst_limit
    metrics_enabled        = false
    logging_level          = "OFF"
  }

  lifecycle {
    precondition {
      condition     = contains(tolist(var.routes), each.key)
      error_message = "method_throttling key \"${each.key}\" is not one of this API's routes."
    }
  }
}

resource "aws_lambda_permission" "this" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*/*"
}

# Optional -- same pattern as infra/modules/static-site's web_acl_id.
# This is the entire reason this module exists rather than staying on
# HTTP API: REST API stage ARNs (unlike apigatewayv2 stage ARNs) are a
# WAFv2-supported association target.
resource "aws_wafv2_web_acl_association" "this" {
  # From the flag, not from var.web_acl_id: the ARN can be unknown until apply (see the variable).
  count = var.associate_web_acl ? 1 : 0

  resource_arn = aws_api_gateway_stage.this.arn
  web_acl_arn  = var.web_acl_id
}
