# =============================================================================
# Alexa+ account linking: what an MCP client needs to find out how to sign in, and the Cognito
# app client Alexa signs in with (docs/enhancements/alexa-plus.md, section 4.2).
#
# The MCP authorization spec (2025-11-25, the version the Alexa+ toolkit speaks) has a client
# discover the authorization server from the resource server:
#
#   1. a request without a token gets a 401 whose WWW-Authenticate header names the resource's
#      Protected Resource Metadata (RFC 9728);
#   2. that document names the authorization server; the client reads its metadata (RFC 8414, or
#      OpenID discovery) and must find S256 in code_challenge_methods_supported;
#   3. the client signs the user in with the authorization code grant and PKCE.
#
# Two things stop Cognito doing this by itself, so both documents are ours:
#
#   - Cognito's OpenID discovery document does not list code_challenge_methods_supported, though
#     it enforces S256, and a client that follows the spec refuses it.
#   - On an execute-api URL the first path segment is the stage, so the spec's root paths
#     (https://<host>/.well-known/...) ask API Gateway for a stage called ".well-known". The
#     documents live under the stage instead, and the 401 header points at the first one; a
#     client must use that header before it tries the root.
#
# The issuer they describe is the stage's own URL. Its endpoints are Cognito's hosted domain:
# the sign-in page, the token exchange and the revocation are all Cognito's. The Alexa client
# asks for no `openid` scope, so no ID token (whose `iss` is Cognito's own) is ever issued to it.
#
# Both documents are static JSON from API Gateway mock integrations: no Lambda, no IAM, no cost,
# and nothing in them is secret (endpoints and a scope name, as every OAuth server publishes).
# They are always there, linked or not; only the Alexa app client depends on a setting.
# =============================================================================

locals {
  stage_url = "https://${local.api_host}/${var.stage_name}"
  mcp_url   = "${local.stage_url}/mcp"

  hosted_ui_base = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${local.aws_region}.amazoncognito.com"

  well_known = {
    # RFC 9728. `resource` is exactly the URL a client sends its requests to.
    "oauth-protected-resource" = {
      resource                 = local.mcp_url
      authorization_servers    = [local.stage_url]
      scopes_supported         = [local.read_scope]
      bearer_methods_supported = ["header"]
      resource_name            = "BloggerBear operator assistant (${var.environment_name})"
    }
    # RFC 8414, and the same document as OpenID discovery under the issuer's path: the one of
    # the spec's discovery URLs that an issuer with a path can serve without the host's root.
    "oauth-authorization-server" = local.authorization_server_metadata
    "openid-configuration"       = local.authorization_server_metadata
  }

  authorization_server_metadata = {
    issuer                                = local.stage_url
    authorization_endpoint                = "${local.hosted_ui_base}/oauth2/authorize"
    token_endpoint                        = "${local.hosted_ui_base}/oauth2/token"
    revocation_endpoint                   = "${local.hosted_ui_base}/oauth2/revoke"
    jwks_uri                              = "https://cognito-idp.${local.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/jwks.json"
    response_types_supported              = ["code"]
    grant_types_supported                 = ["authorization_code", "refresh_token"]
    code_challenge_methods_supported      = ["S256"]
    token_endpoint_auth_methods_supported = ["client_secret_basic", "client_secret_post", "none"]
    scopes_supported                      = [local.read_scope]
    subject_types_supported               = ["public"]
    id_token_signing_alg_values_supported = ["RS256"]
  }

  # The WWW-Authenticate header every 401 from this API carries (agent.tf's gateway responses).
  # API Gateway writes a fixed header value in single quotes.
  www_authenticate = {
    "gatewayresponse.header.WWW-Authenticate" = "'Bearer resource_metadata=\"${local.stage_url}/.well-known/oauth-protected-resource\", scope=\"${local.read_scope}\"'"
  }

  alexa_enabled = length(var.alexa_redirect_uris) > 0

  # Folded into the stage's redeployment (main.tf): a new or changed document must reach the
  # stage, which only a new deployment does.
  alexa_redeployment = {
    well_known = aws_api_gateway_resource.well_known.id
    documents = {
      for name, resource in aws_api_gateway_resource.well_known_document : name => {
        resource    = resource.id
        method      = aws_api_gateway_method.well_known[name].id
        integration = aws_api_gateway_integration.well_known[name].id
        body        = aws_api_gateway_integration_response.well_known[name].response_templates
      }
    }
  }
}

resource "aws_api_gateway_resource" "well_known" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = ".well-known"
}

resource "aws_api_gateway_resource" "well_known_document" {
  for_each = local.well_known

  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.well_known.id
  path_part   = each.key
}

# Public, as metadata must be: the client reads it before it has a token.
resource "aws_api_gateway_method" "well_known" {
  # checkov:skip=CKV_AWS_59:OAuth metadata documents, public by RFC 8414/9728: a client reads them before it has a token
  for_each = local.well_known

  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = aws_api_gateway_resource.well_known_document[each.key].id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "well_known" {
  for_each = local.well_known

  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = aws_api_gateway_resource.well_known_document[each.key].id
  http_method = aws_api_gateway_method.well_known[each.key].http_method
  type        = "MOCK"

  # A mock integration answers whatever status its request template names.
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
  passthrough_behavior = "WHEN_NO_TEMPLATES"
}

resource "aws_api_gateway_method_response" "well_known" {
  for_each = local.well_known

  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = aws_api_gateway_resource.well_known_document[each.key].id
  http_method = aws_api_gateway_method.well_known[each.key].http_method
  status_code = "200"

  response_models = {
    "application/json" = "Empty"
  }
  response_parameters = {
    "method.response.header.Cache-Control" = true
  }
}

resource "aws_api_gateway_integration_response" "well_known" {
  for_each = local.well_known

  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = aws_api_gateway_resource.well_known_document[each.key].id
  http_method = aws_api_gateway_method.well_known[each.key].http_method
  status_code = aws_api_gateway_method_response.well_known[each.key].status_code

  # The response template is VTL: a `$` or a `#` in it would be read as code. None of the values
  # above can hold either (URLs Terraform builds, and fixed words); the module's test holds that.
  response_templates = {
    "application/json" = jsonencode(each.value)
  }
  response_parameters = {
    "method.response.header.Cache-Control" = "'public, max-age=300'"
  }

  depends_on = [aws_api_gateway_integration.well_known]
}

# The app client Alexa signs in with: only where Alexa's redirect URLs have been given (they come
# from `alexa-ai configure-account-linking`, alexa/README.md). Its own client so that it can be
# revoked alone, and so that its refresh token can outlive the page's one day: a linked device
# would otherwise need linking again every day.
resource "aws_cognito_user_pool_client" "alexa" {
  count = local.alexa_enabled ? 1 : 0

  name         = "${local.name}-alexa"
  user_pool_id = aws_cognito_user_pool.this.id

  # Alexa's account linking authenticates to the token endpoint with a client secret, on top of
  # PKCE. var.alexa_client_secret turns it off for a toolkit that registers a public client.
  generate_secret = var.alexa_client_secret

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  # The one scope the routes require, and not `openid`: Alexa needs an access token, not an ID
  # token issued by an issuer other than the one the metadata above names.
  allowed_oauth_scopes         = [local.read_scope]
  supported_identity_providers = ["COGNITO"]
  callback_urls                = var.alexa_redirect_uris

  # No password or SRP flow: the only way in is the hosted sign-in page (MFA where the pool
  # requires it), then refreshing what it issued.
  explicit_auth_flows           = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = var.alexa_refresh_token_days
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  depends_on = [aws_cognito_resource_server.ops]
}
