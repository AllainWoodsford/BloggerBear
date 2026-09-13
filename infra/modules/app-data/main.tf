# -----------------------------------------------------------------------
# Phase 1 application data tables. All PAY_PER_REQUEST (no capacity
# planning for a single-operator portfolio project) -- see
# docs/project-plan.md §5 for the data model this mirrors. Only the 5
# tables Phase 1 actually reads/writes are created here (Topics,
# Findings, CandidateIdeas, Articles, ModerationQueue); the remaining
# tables listed in §5 (ViewCounters, Feedback, PromptRefinements) are
# Phase 2+ and deliberately not created yet.
# -----------------------------------------------------------------------

resource "aws_dynamodb_table" "topics" {
  name         = "bloggerbear-${var.environment_name}-topics"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "topic_id"

  attribute {
    name = "topic_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "findings" {
  name         = "bloggerbear-${var.environment_name}-findings"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "topic_id"
  range_key    = "captured_at"

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
  hash_key     = "topic_id"
  range_key    = "created_at"

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
  hash_key     = "article_id"

  attribute {
    name = "article_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "moderation_queue" {
  name         = "bloggerbear-${var.environment_name}-moderation-queue"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "queue_id"

  attribute {
    name = "queue_id"
    type = "S"
  }
}
