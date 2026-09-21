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
# version rather than an overwrite, so history is preserved. No TTL --
# refinement history is meant to persist, not expire.
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
}

# Admin-visibility record of daily_cycle Step Functions executions that
# exhausted their retries and landed on pipeline_dlq -- one item per
# dlq_handler.py invocation (see lambdas/dlq_handler.py). No TTL -- these
# are meant to persist for later review, not expire like Findings.
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
