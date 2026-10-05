# Alexa+ account linking (alexa.tf), planned with a mocked AWS provider like the other tests here.
# What is held: the metadata documents say what the MCP authorization spec needs them to say (the
# resource, the issuer, S256), they are public static JSON with nothing a template could run, every
# 401 points at them, and the Alexa app client exists only where its redirect URLs are given and
# can get the one scope the routes require and nothing more.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111111111111"
    }
  }
}

# Known ids, so the documents' URLs are plain strings at plan time.
override_resource {
  target          = aws_api_gateway_rest_api.this
  override_during = plan
  values = {
    id               = "abc123"
    root_resource_id = "root1"
    execution_arn    = "arn:aws:execute-api:ap-southeast-2:111111111111:abc123"
  }
}

override_resource {
  target          = aws_cognito_user_pool.this
  override_during = plan
  values = {
    id  = "ap-southeast-2_TEST"
    arn = "arn:aws:cognito-idp:ap-southeast-2:111111111111:userpool/ap-southeast-2_TEST"
  }
}

variables {
  # What a root passes when UNIQUE_NAME_PREFIX is not set: the original deployment's prefix.
  unique_name_prefix = "bloggerbear"

  # The roots' provider default_tags, without Environment and TerraformRoot.
  default_tags = {
    ManagedBy = "Terraform"
    Project   = "BloggerBear"
  }

  aws_region       = "ap-southeast-2"
  environment_name = "test"
  tables = {
    MODEL_CONFIG_TABLE = {
      name = "bloggerbear-test-model-config"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config"
    }
  }
  content_bucket_name     = "bloggerbear-test-content"
  content_bucket_arn      = "arn:aws:s3:::bloggerbear-test-content"
  stage_name              = "test"
  hosted_ui_domain_prefix = "bloggerbear-test-ops"
  callback_urls           = ["https://example.com/ask.html"]
  logout_urls             = ["https://example.com/ask.html"]
  mfa_configuration       = "OPTIONAL"
  throttling_rate_limit   = 5
  throttling_burst_limit  = 10
  agent_model_id          = "au.example.test-model-v1:0"
  agent_allowed_origin    = "https://example.com"
}

run "the_metadata_documents_say_what_the_spec_needs" {
  command = plan

  assert {
    condition     = toset(keys(aws_api_gateway_resource.well_known_document)) == toset(["oauth-protected-resource", "oauth-authorization-server", "openid-configuration"])
    error_message = "the protected resource's metadata, the authorization server's, and the same as OpenID discovery"
  }

  assert {
    condition     = aws_api_gateway_resource.well_known.path_part == ".well-known" && aws_api_gateway_resource.well_known.parent_id == "root1"
    error_message = "the documents live under /<stage>/.well-known/"
  }

  assert {
    condition = jsondecode(aws_api_gateway_integration_response.well_known["oauth-protected-resource"].response_templates["application/json"]) == {
      resource                 = "https://abc123.execute-api.ap-southeast-2.amazonaws.com/test/mcp"
      authorization_servers    = ["https://abc123.execute-api.ap-southeast-2.amazonaws.com/test"]
      scopes_supported         = ["bloggerbear-ops/read"]
      bearer_methods_supported = ["header"]
      resource_name            = "BloggerBear operator assistant (test)"
    }
    error_message = "the PRM names exactly the MCP URL as the resource, the stage as the issuer, and the one scope"
  }

  assert {
    condition = alltrue([
      for name in ["oauth-authorization-server", "openid-configuration"] :
      jsondecode(aws_api_gateway_integration_response.well_known[name].response_templates["application/json"]).code_challenge_methods_supported == ["S256"]
    ])
    error_message = "the authorization server must advertise S256, which Cognito's own discovery does not"
  }

  assert {
    condition     = jsondecode(aws_api_gateway_integration_response.well_known["oauth-authorization-server"].response_templates["application/json"]).issuer == "https://abc123.execute-api.ap-southeast-2.amazonaws.com/test" && jsondecode(aws_api_gateway_integration_response.well_known["oauth-authorization-server"].response_templates["application/json"]).authorization_endpoint == "https://bloggerbear-test-ops.auth.ap-southeast-2.amazoncognito.com/oauth2/authorize" && jsondecode(aws_api_gateway_integration_response.well_known["oauth-authorization-server"].response_templates["application/json"]).token_endpoint == "https://bloggerbear-test-ops.auth.ap-southeast-2.amazoncognito.com/oauth2/token"
    error_message = "the issuer is the one the PRM names, and the endpoints are Cognito's hosted domain"
  }

  # A response template is VTL: `$` and `#` would be code.
  assert {
    condition = alltrue([
      for response in aws_api_gateway_integration_response.well_known :
      !strcontains(response.response_templates["application/json"], "$") && !strcontains(response.response_templates["application/json"], "#")
    ])
    error_message = "a metadata document must not contain $ or #, which API Gateway would read as VTL"
  }

  assert {
    condition = alltrue([
      for method in aws_api_gateway_method.well_known :
      method.http_method == "GET" && method.authorization == "NONE"
    ])
    error_message = "metadata is read before there is a token: GET, and public"
  }

  assert {
    condition     = alltrue([for integration in aws_api_gateway_integration.well_known : integration.type == "MOCK"])
    error_message = "the documents are static: a mock integration, no Lambda"
  }
}

run "every_401_says_where_the_metadata_is" {
  command = plan

  assert {
    condition     = aws_api_gateway_gateway_response.cors["UNAUTHORIZED"].response_parameters["gatewayresponse.header.WWW-Authenticate"] == "'Bearer resource_metadata=\"https://abc123.execute-api.ap-southeast-2.amazonaws.com/test/.well-known/oauth-protected-resource\", scope=\"bloggerbear-ops/read\"'"
    error_message = "the 401 must name the PRM in WWW-Authenticate (RFC 9728), as API Gateway writes a fixed value"
  }

  assert {
    condition = alltrue([
      for type, response in aws_api_gateway_gateway_response.cors :
      !contains(keys(response.response_parameters), "gatewayresponse.header.WWW-Authenticate") if type != "UNAUTHORIZED"
    ])
    error_message = "only the 401 carries WWW-Authenticate"
  }
}

run "no_alexa_client_until_its_redirect_urls_are_given" {
  command = plan

  assert {
    condition     = length(aws_cognito_user_pool_client.alexa) == 0
    error_message = "with no Alexa redirect URLs nothing can link an Alexa account"
  }

  assert {
    condition     = output.alexa_client_id == null
    error_message = "no client, no id"
  }
}

run "the_alexa_client_gets_the_one_scope_through_the_hosted_page_only" {
  command = plan

  variables {
    alexa_redirect_uris = ["https://pitangui.amazon.com/api/skill/link/TEST", "https://layla.amazon.com/api/skill/link/TEST"]
  }

  assert {
    condition     = length(aws_cognito_user_pool_client.alexa) == 1
    error_message = "redirect URLs given: one Alexa client"
  }

  assert {
    condition     = toset(aws_cognito_user_pool_client.alexa[0].allowed_oauth_scopes) == toset(["bloggerbear-ops/read"])
    error_message = "the Alexa client may ask for the routes' scope and nothing else, not even openid"
  }

  assert {
    condition     = aws_cognito_user_pool_client.alexa[0].allowed_oauth_flows == toset(["code"]) && aws_cognito_user_pool_client.alexa[0].allowed_oauth_flows_user_pool_client
    error_message = "the authorization code grant only"
  }

  assert {
    condition     = aws_cognito_user_pool_client.alexa[0].explicit_auth_flows == toset(["ALLOW_REFRESH_TOKEN_AUTH"])
    error_message = "no password or SRP sign-in: the hosted page (with MFA where required) is the only way in"
  }

  assert {
    condition     = toset(aws_cognito_user_pool_client.alexa[0].callback_urls) == toset(var.alexa_redirect_uris)
    error_message = "Cognito may send the code back to Alexa's redirect URLs and nowhere else"
  }

  assert {
    condition     = aws_cognito_user_pool_client.alexa[0].generate_secret && aws_cognito_user_pool_client.alexa[0].refresh_token_validity == 30 && aws_cognito_user_pool_client.alexa[0].enable_token_revocation
    error_message = "a confidential client, refresh for 30 days, revocable"
  }

  assert {
    condition     = aws_cognito_user_pool_client.alexa[0].user_pool_id == "ap-southeast-2_TEST"
    error_message = "the Alexa client belongs to this environment's own pool"
  }
}

run "an_alexa_redirect_url_must_be_https" {
  command = plan

  variables {
    alexa_redirect_uris = ["http://pitangui.amazon.com/api/skill/link/TEST"]
  }

  expect_failures = [var.alexa_redirect_uris]
}
