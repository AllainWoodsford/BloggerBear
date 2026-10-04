# -----------------------------------------------------------------------
# One environment each: the dev assistant reads dev's things, production's reads production's.
# Design: docs/enhancements/alexa-plus-operator-assistant-enhancement.md, section 6, "What each
# environment's assistant can read".
#
# Dev and production are one AWS account, told apart by names and by IAM. The separation is built
# in four layers, and this file is the second:
#
# 1. Allow by name. Each role's Allow statements (main.tf, memory.tf, agent.tf) name this
#    environment's tables, bucket prefix and log group, by ARN. That is the real wall, and it was
#    already there.
# 2. Deny by tag (below). A net under layer 1: if an Allow is ever widened by mistake, or a
#    caller hands the module the other environment's ARN, a resource tagged for the other
#    environment is still refused, wherever the service tells IAM the resource's tags.
# 3. Filter in code where IAM cannot help. CloudWatch lists every alarm in the account, so the
#    alarms tool asks only for "bloggerbear-<environment>-" (lambdas/ops_mcp/account.py).
# 4. Account-wide data only where the caller says so (var.account_wide_data, variables.tf): the
#    AWS bill is the whole account's and cannot be split, so only production's assistant has it.
# -----------------------------------------------------------------------

# The same statement for both roles: everything is refused on a resource that carries an
# Environment tag naming an environment other than this one.
#
# Both conditions are needed, and a statement's conditions are ANDed:
#
# - Null = false: the request carries the resource's Environment tag at all. Without this line the
#   statement would deny nearly everything: StringNotEquals is true when the key is absent, and
#   most requests carry no resource tag (the service does not supply one, or the resource is
#   untagged). With it, the statement does nothing unless the service puts the tag in front of
#   IAM, so it cannot break a tool; it can only refuse a resource that says it is someone else's.
# - StringNotEquals: and that tag is not this environment's name.
#
# Every resource Terraform makes carries Environment from the provider's default_tags
# (infra/environments/*/main.tf, locals.default_tags): "dev", "production", or "shared" for
# infra/bootstrap. The values compared against are locals.readable_environments (main.tf), which
# start with var.environment_name, so the calling root's tag
# and the name it passes here must be the same word: test_terraform_wiring.py holds dev's.
#
# WHAT IT CAN ACTUALLY AFFECT. Looked up 2026-10-04 in the AWS Service Authorization Reference,
# for each action these two roles are allowed (the reference pages are
# docs.aws.amazon.com/service-authorization/latest/reference/list_amazondynamodb.html,
# list_amazoncloudwatchlogs.html, list_amazons3.html, list_amazoncloudwatch.html,
# list_amazonbedrock.html and list_awslambda.html; the tables were read from the machine-readable
# copy of the same reference, servicereference.us-east-1.amazonaws.com/v1/<service>/<service>.json).
# A condition key works for an action only when it is listed for that action or for the resource
# type the action is on.
#
#   DynamoDB    GetItem, Query, Scan, BatchGetItem, PutItem, UpdateItem, DeleteItem are on
#               `table` (Query and Scan also on `index`), and both resource types list
#               aws:ResourceTag/${TagKey}. An index inherits its table's tags. SUPPORTED, but it
#               may need switching on: the DynamoDB guide ("Enabling ABAC in DynamoDB") says "for
#               most of the AWS accounts, ABAC is enabled by default", and that where it is not,
#               tag conditions "are evaluated as if no tags are present". So in an account
#               without it this statement does nothing for DynamoDB (it fails open, not shut).
#               Whether this account has it is NOT checked by anything here: the DynamoDB
#               console's Settings page shows it (dynamodb:GetAbacStatus), and it is enabled per
#               region.
#   Logs        CreateLogStream and PutLogEvents are on `log-stream`, which lists
#               aws:ResourceTag/${TagKey}. LISTED AS SUPPORTED. A stream cannot be tagged itself;
#               the reference does not say whose tags are compared, and the log group's is the
#               only candidate.
#   S3          GetObject is on `object`, which lists aws:ResourceTag/${TagKey} and
#               s3:BucketTag/${TagKey}. LISTED, BUT OFF BY DEFAULT: the S3 guide ("Enabling ABAC
#               in general purpose buckets") says "by default, ABAC is disabled for all Amazon S3
#               general purpose buckets". Nothing in this project enables it on the content
#               bucket, so today this statement does nothing for S3.
#   CloudWatch  DescribeAlarms is on `alarm`, which lists aws:ResourceTag/${TagKey}. But the
#               alarms tool lists by name prefix, a call that is about no one alarm and is
#               authorized against "alarm:*" (see DescribeAlarms in main.tf). There is no single
#               resource whose tag could be compared, so this statement is NOT RELIED ON for
#               alarms. That is why the tool filters by name in code.
#   Bedrock     InvokeModel on `foundation-model` and `inference-profile` (the system-defined
#               profiles the agent is allowed): neither resource type lists any condition key.
#               NOT SUPPORTED, so the statement does nothing. (Application inference profiles,
#               which an account makes and can tag, do list it; the agent's Allow does not cover
#               them.)
#   Lambda      The Web Adapter layer: `layerVersion` lists no condition key. NOT SUPPORTED. It
#               would not matter if it were: neither role is allowed any Lambda action, and the
#               layer is read by whoever creates the function (the deploy role), not by the
#               function's own role.
#
# So the honest summary: today this is a net under the DynamoDB statements (if the account has
# tag-based access on), probably under the log statements, and under nothing else.
#
# COULD IT REFUSE SOMETHING THE ROLES NEED? Each thing they are allowed, and the tag it carries:
#
# - The app tables, the content bucket, the config table: made by the root that calls this
#   module, so tagged with this environment. Not refused.
# - The assistant's own suggestions table (memory.tf) and the two functions' log groups: made by
#   this module under the caller's provider, so tagged with this environment. Not refused.
# - Bedrock's foundation models are AWS's own and carry no tag of ours; the system-defined
#   inference profile is likewise untagged, and no tag is supplied for either. Not refused.
# - The Web Adapter layer belongs to the adapter project's own AWS account and is not read by these
#   roles at all. Not refused.
# - Alarms: a list by prefix carries no resource tag. Not refused (and not narrowed either).
# - Resources tagged Environment = "shared" (infra/bootstrap: the deploy roles, the state bucket,
#   the API Gateway account role): the owner's rule is that production's assistant may read
#   what is shared and dev's may not. So the Deny refuses everything outside
#   locals.readable_environments (main.tf): for dev, anything not "dev"; for production,
#   anything neither "production" nor "shared". Production's role is still allowed nothing on a
#   shared resource except what its Allows name (table_sample's tag-conditioned reads).
#
# A Deny, in a policy of its own, so that each role's Allow policy stays what its tests say it
# is: a list of what is allowed, with no wildcard in it. The "*" here grants nothing.
data "aws_iam_policy_document" "other_environments_denied" {
  statement {
    sid       = "DenyOtherEnvironments"
    effect    = "Deny"
    actions   = ["*"]
    resources = ["*"]

    condition {
      test     = "Null"
      variable = "aws:ResourceTag/Environment"
      values   = ["false"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "aws:ResourceTag/Environment"
      values   = local.readable_environments
    }
  }
}

# On the MCP server's role (main.tf): the one that reads tables, article bodies and alarms.
resource "aws_iam_role_policy" "ops_mcp_other_environments_denied" {
  name   = "${local.name}-other-environments-denied"
  role   = aws_iam_role.ops_mcp.id
  policy = data.aws_iam_policy_document.other_environments_denied.json
}

# On the agent's role (agent.tf): it reads one row and calls the model, so there is little for
# this to catch, but the rule is one rule for the whole assistant.
resource "aws_iam_role_policy" "ops_agent_other_environments_denied" {
  name   = "${local.agent_name}-other-environments-denied"
  role   = aws_iam_role.ops_agent.id
  policy = data.aws_iam_policy_document.other_environments_denied.json
}
