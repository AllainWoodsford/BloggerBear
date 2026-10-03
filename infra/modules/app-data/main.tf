# -----------------------------------------------------------------------
# Phase 1 application data tables. All PAY_PER_REQUEST (no capacity
# planning for a single-operator portfolio project) -- see
# docs/project-plan.md §5 for the data model this mirrors. Only the 5
# tables Phase 1 actually reads/writes are created here (Topics,
# Findings, CandidateIdeas, Articles, ModerationQueue); the remaining
# tables listed in §5 (ViewCounters, Feedback, PromptRefinements) are
# Phase 2+ and deliberately not created yet.
#
# Phase 5 adds the two below (Feedback, PromptRefinements) for the
# weekly reflection job -- see docs/project-plan.md §5. ViewCounters was
# already folded into the Articles table's own view-count attribute in
# Phase 4 rather than getting a dedicated table, so it never appears here.
# -----------------------------------------------------------------------

resource "aws_dynamodb_table" "topics" {
  name         = "bloggerbear-${var.environment_name}-topics"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "topic_id"

  attribute {
    name = "topic_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "findings" {
  name         = "bloggerbear-${var.environment_name}-findings"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key  = "topic_id"
  range_key = "captured_at"

  attribute {
    name = "topic_id"
    type = "S"
  }

  attribute {
    name = "captured_at"
    type = "S"
  }

  # Findings are rolling research history, not permanent records -- items
  # expire via TTL once the common/ Python code sets expires_at on write.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "candidate_ideas" {
  name         = "bloggerbear-${var.environment_name}-candidate-ideas"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key  = "topic_id"
  range_key = "created_at"

  attribute {
    name = "topic_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # Cleanup PR: a pure operational list (the pipeline's own ideation scratchpad, superseded by
  # each topic's next research cycle) -- nothing aggregates it across history, so it expires via
  # TTL once common/dynamo.py's put_candidate_idea sets expires_at on write, same as Findings.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "articles" {
  name         = "bloggerbear-${var.environment_name}-articles"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "article_id"

  attribute {
    name = "article_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "topic_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # Scaling PR A: the public API lists published articles (home page, topic pages, RSS) on
  # nearly every request, which used to Scan the whole table and filter. These two indexes let
  # common/dynamo.py Query instead. `created_at`, not `published_at`, is the sort key because
  # put_article stores published_at as an explicit null until an article is published, and
  # DynamoDB rejects a write whose index key attribute holds a null. Every writer goes through
  # put_article, which always sets status, topic_id and created_at, so no article is left out
  # of either index. ALL projection: callers need whole items (titles, lineage, vote counts),
  # and articles are small since their bodies live in S3.
  #
  # Adding both to an existing table is fine in one apply: the AWS provider (checked against
  # v6.64.0's table.go) sends one UpdateTable per new index and waits for each to become ACTIVE
  # before starting the next, which is what DynamoDB requires.
  global_secondary_index {
    name            = "by_status_created_at"
    hash_key        = "status"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  # Topic pages and the daily cycle's per-topic lookups (recent titles, top-voted example)
  # read one topic's articles, then keep the published ones.
  global_secondary_index {
    name            = "by_topic_created_at"
    hash_key        = "topic_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }
}

resource "aws_dynamodb_table" "moderation_queue" {
  name         = "bloggerbear-${var.environment_name}-moderation-queue"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "queue_id"

  attribute {
    name = "queue_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "article_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # Scaling PR A: the review inbox (pending items), a topic page's "pending review" count and
  # the stuck-rewrite sweep all ask for items in one status; this replaces their Scan + filter.
  # put_moderation_item always sets status and created_at, so every item is indexed.
  global_secondary_index {
    name            = "by_status_created_at"
    hash_key        = "status"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  # Finding an article's live queue item (the newest one: a Re-Write leaves the old item behind
  # as history). KEYS_ONLY is enough -- common/dynamo.py takes the newest queue_id from here and
  # reads the item itself with a strongly consistent GetItem, since index reads can lag the
  # table and the caller acts on the item's status.
  global_secondary_index {
    name            = "by_article_created_at"
    hash_key        = "article_id"
    range_key       = "created_at"
    projection_type = "KEYS_ONLY"
  }

  # Cleanup PR: common/dynamo.py's update_moderation_status sets expires_at only when an item
  # is rejected (never on approve/pending), so a rejected item self-clears via TTL and a pending
  # or approved one never does. Flagged as a real trade-off, not a free cleanup, in the PR that
  # added this: admin_api_handler.py's _moderation_queue_stats reads rejected items' reasons
  # "across all history" to inform compliance-prompt iteration -- that history will only reach
  # back as far as this TTL window from here on. See that function's own updated docstring.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# Phase 5: reader feedback (thumbs up/down + optional comment) on published
# articles, submitted anonymously via the public API's
# POST /articles/{article_id}/feedback route (see lambdas/common/dynamo.py's
# put_feedback). No TTL -- unlike Findings, feedback is a permanent record
# the weekly reflection job reads back historically, not rolling research
# history.
resource "aws_dynamodb_table" "feedback" {
  name         = "bloggerbear-${var.environment_name}-feedback"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key  = "article_id"
  range_key = "feedback_id"

  attribute {
    name = "article_id"
    type = "S"
  }

  attribute {
    name = "feedback_id"
    type = "S"
  }
}

# Phase 5: versioned per-topic prompt refinements the weekly reflection job
# writes after analyzing a topic's recent feedback -- each write is a new
# version rather than an overwrite, so history is preserved. An approved or
# equipped version is meant to persist, not expire -- see the ttl block
# below for what changed in the Cleanup PR.
resource "aws_dynamodb_table" "prompt_refinements" {
  name         = "bloggerbear-${var.environment_name}-prompt-refinements"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key  = "topic_id"
  range_key = "version"

  attribute {
    name = "topic_id"
    type = "S"
  }

  attribute {
    name = "version"
    type = "S"
  }

  # Cleanup PR: common/dynamo.py's update_prompt_refinement_status sets expires_at only when a
  # version is rejected, never on approve/equip, so a rejected version self-clears via TTL while
  # real, adopted history persists exactly as before. Nothing reads a rejected version back
  # historically, unlike moderation_queue's rejected items above -- safe with no follow-on effect.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# Admin-visibility record of daily_cycle Step Functions executions that
# exhausted their retries and landed on pipeline_dlq -- one item per
# dlq_handler.py invocation (see lambdas/dlq_handler.py). Originally kept
# without a TTL ("meant to persist for later review, not expire like
# Findings") -- the Cleanup PR revisits that: nothing aggregates these
# across history the way _moderation_queue_stats does for
# ModerationQueue, so a bounded operational window is safe here, same as
# CandidateIdeas above.
resource "aws_dynamodb_table" "failed_executions" {
  name         = "bloggerbear-${var.environment_name}-failed-executions"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "failure_id"

  attribute {
    name = "failure_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# BloggerBear's "musings" feed -- one item per article publish (any of the
# three publish paths, see common/musings.py) plus one every 4 days from
# musing_feedback_handler.py's periodic reflection on reader feedback. No
# TTL -- this is a permanent, readable feed, not a transient record.
resource "aws_dynamodb_table" "musings" {
  name         = "bloggerbear-${var.environment_name}-musings"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "musing_id"

  attribute {
    name = "musing_id"
    type = "S"
  }
}

# AI lineage/cost-tracking enhancement (docs/project-plan.md §11, PR 1 of
# 5): a DynamoDB-backed "supported models" registry, so switching or
# adding a model never needs a Terraform apply -- populated via the admin
# API/CLI (common/dynamo.py's put_model), not hardcoded here. No TTL --
# permanent configuration, not rolling data.
resource "aws_dynamodb_table" "models" {
  name         = "bloggerbear-${var.environment_name}-models"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "model_id"

  attribute {
    name = "model_id"
    type = "S"
  }
}

# Single well-known row (config_id = "default") holding the current
# global default/fallback model IDs -- lets an operator change the
# default model without a Terraform apply. Absence of this row (or of
# this table having any row at all) is a valid, expected state -- model
# resolution falls back to var.bedrock_model_id (see common/bedrock.py's
# resolve_model), never fails closed just because nobody's configured
# this yet.
resource "aws_dynamodb_table" "model_config" {
  name         = "bloggerbear-${var.environment_name}-model-config"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "config_id"

  # The feedback limiter's counters (one row per rate-limit window and per day, see
  # common/feedback_limits.py) carry an expires_at; TTL clears them out. The settings rows
  # ("default", "pipeline", "feedback") have none, so they are never expired.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  attribute {
    name = "config_id"
    type = "S"
  }
}

# Observability enhancement, PR 1: Bedrock usage/cost that isn't part of any one article's lineage
# (musings, the weekly reflection, gear identity, comment screening) plus a few reader-activity
# counters (feedback given/rejected, loot drops) -- see common/stats_tracking.py. One row, key
# "current" (hash key stats_id, a fixed string -- there is only ever one), updated in place all
# week with ADD expressions the same way model_config's rate-limit counters are (common/dynamo.py's
# consume_feedback_counter); the row and every attribute on it come into existence on first use, no
# separate "create the row" step. A later PR's weekly rollover copies this row into
# stats_history below (keyed by the week that just ended) and resets it for the next week.
resource "aws_dynamodb_table" "stats_current" {
  name         = "bloggerbear-${var.environment_name}-stats-current"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "stats_id"

  attribute {
    name = "stats_id"
    type = "S"
  }
}

# One row per completed week (hash key week_start, the Monday it covers, e.g. "2026-09-15"),
# written once by the rollover job (a later PR) and never updated after that -- the same shape as
# stats_current's row, so "what changed this week vs a typical one" is a straight row-to-row
# comparison. Empty until that rollover job exists.
resource "aws_dynamodb_table" "stats_history" {
  name                        = "bloggerbear-${var.environment_name}-stats-history"
  billing_mode                = "PAY_PER_REQUEST"
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "week_start"

  attribute {
    name = "week_start"
    type = "S"
  }
}
