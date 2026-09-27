# -----------------------------------------------------------------------
# Web search through Amazon Bedrock AgentCore: a Gateway with the managed
# Web Search Tool connector as its one target.
#
# The fallback search backend for lambdas/common/web_search.py (provider
# "agentcore"): GDELT, the keyless default, has been timing out and
# rate-limiting requests from Lambda, which left topics that depend on it
# with no findings at all. This is Amazon's own web index (titles, URLs,
# snippets and publish dates), called with plain SigV4-signed MCP
# requests -- no API key, no secret, no agent framework.
#
# Its own Region (var.region), not the stack's: the connector is only
# offered in us-east-1, eu-west-1 and ap-northeast-1. Tokyo is the
# closest to Sydney; the Lambdas simply call across Regions. Every
# resource here sets `region` itself (AWS provider 6.x), so no aliased
# provider is needed.
#
# Priced per query (see the AgentCore pricing page) -- which is why the
# app only falls back to it when GDELT fails, unless a topic asks for it
# directly.
# -----------------------------------------------------------------------

data "aws_caller_identity" "current" {}

# The role the gateway assumes to call the connector ("gateway execution
# role"). Trust is scoped to this account's gateways in this Region.
data "aws_iam_policy_document" "gateway_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${var.name}-agentcore-gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_trust.json
}

# The two permissions the Web Search Tool connector documents for the
# gateway execution role. InvokeWebSearch is checked against a
# service-owned ARN ("aws" in the account field), one per Region.
data "aws_iam_policy_document" "gateway" {
  statement {
    sid       = "InvokeGateway"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/*"]
  }
  statement {
    sid       = "InvokeWebSearch"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeWebSearch"]
    resources = ["arn:aws:bedrock-agentcore:${var.region}:aws:tool/web-search.v1"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "${var.name}-agentcore-gateway"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

# IAM (SigV4) inbound auth: only principals granted
# bedrock-agentcore:InvokeGateway on this gateway (the Lambda exec role,
# via the `gateway_arn` output) can call it.
resource "aws_bedrockagentcore_gateway" "this" {
  name            = "${var.name}-web-search"
  region          = var.region
  role_arn        = aws_iam_role.gateway.arn
  protocol_type   = "MCP"
  authorizer_type = "AWS_IAM"

  # The MCP protocol version lambdas/common/web_search.py's AgentCoreProvider
  # sends (MCP_PROTOCOL_VERSION) -- stated here rather than relying on the
  # service's default list. Change both together.
  protocol_configuration {
    mcp {
      supported_versions = ["2025-11-25"]
    }
  }

  depends_on = [aws_iam_role_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "web_search" {
  name               = var.target_name
  region             = var.region
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id

  target_configuration {
    mcp {
      connector {
        # Pinned: 1.2.0 is the first version with the per-request
        # published-date filter the app relies on for its "last N hours"
        # window.
        source {
          connector_id = "web-search"
          version      = "1.2.0"
        }
        configuration {
          name             = "WebSearch"
          parameter_values = jsonencode({})
        }
      }
    }
  }

  # Connector targets support only the gateway's own IAM role.
  credential_provider_configuration {
    gateway_iam_role {}
  }
}
