# The prompts bucket, its hash table and what the shared Lambda role may do with them (main.tf),
# planned with a mocked AWS provider like the ops-assistant module's tests. What is held: the
# bucket's settings; a bucket policy only when a publisher is named, refusing every other writer
# through a condition on aws:PrincipalArn (never a Principal naming the role, which S3 refuses
# while the role does not exist); and lambda_exec reading, never writing, with GetItem the one
# right it has on the table. The prefix is another deployment's, so every name is seen to follow it.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

override_resource {
  target          = aws_s3_bucket.prompts
  override_during = plan
  values = {
    arn = "arn:aws:s3:::acme-blog-dev-prompts"
    id  = "acme-blog-dev-prompts"
  }
}

override_resource {
  target          = aws_dynamodb_table.prompt_hashes
  override_during = plan
  values = {
    arn = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/acme-blog-dev-prompt-hashes"
  }
}

variables {
  unique_name_prefix  = "acme-blog"
  environment_name    = "dev"
  lambda_exec_role_id = "acme-blog-dev-lambda-exec"
}

run "the_bucket_is_private_versioned_encrypted_and_tidies_itself" {
  command = plan

  assert {
    condition     = aws_s3_bucket.prompts.bucket == "acme-blog-dev-prompts" && aws_dynamodb_table.prompt_hashes.name == "acme-blog-dev-prompt-hashes"
    error_message = "<prefix>-<env>-prompts and <prefix>-<env>-prompt-hashes, from the variables"
  }

  assert {
    condition     = aws_s3_bucket_versioning.prompts.versioning_configuration[0].status == "Enabled"
    error_message = "versioned"
  }

  assert {
    condition     = one([for rule in aws_s3_bucket_server_side_encryption_configuration.prompts.rule : one(rule.apply_server_side_encryption_by_default).sse_algorithm]) == "AES256"
    error_message = "SSE-S3"
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.prompts.block_public_acls
      && aws_s3_bucket_public_access_block.prompts.block_public_policy
      && aws_s3_bucket_public_access_block.prompts.ignore_public_acls
      && aws_s3_bucket_public_access_block.prompts.restrict_public_buckets
      && aws_s3_bucket_ownership_controls.prompts.rule[0].object_ownership == "BucketOwnerEnforced"
    )
    error_message = "no public access of any kind, and no ACLs"
  }

  assert {
    condition = (
      length(aws_s3_bucket_lifecycle_configuration.prompts.rule) == 1
      && aws_s3_bucket_lifecycle_configuration.prompts.rule[0].status == "Enabled"
      && aws_s3_bucket_lifecycle_configuration.prompts.rule[0].noncurrent_version_expiration[0].noncurrent_days == 90
      && aws_s3_bucket_lifecycle_configuration.prompts.rule[0].abort_incomplete_multipart_upload[0].days_after_initiation == 7
    )
    error_message = "replaced files are kept 90 days, and an interrupted upload's parts 7"
  }

  assert {
    condition     = aws_dynamodb_table.prompt_hashes.hash_key == "prompt_hash" && length(aws_dynamodb_table.prompt_hashes.ttl) == 0
    error_message = "keyed by the file's sha256, and no TTL: a retired row is the record of an approval"
  }
}

run "dev_can_be_destroyed_with_its_prompts_in_it" {
  command = plan

  assert {
    condition     = aws_s3_bucket.prompts.force_destroy && !aws_dynamodb_table.prompt_hashes.deletion_protection_enabled
    error_message = "dev (protect_data unset): the bucket and the table can be destroyed"
  }
}

run "production_protects_the_table_and_keeps_the_bucket" {
  command = plan

  variables {
    environment_name = "production"
    protect_data     = true
  }

  assert {
    condition = (
      aws_s3_bucket.prompts.force_destroy == false
      && aws_dynamodb_table.prompt_hashes.deletion_protection_enabled
      && aws_dynamodb_table.prompt_hashes.point_in_time_recovery[0].enabled
      && aws_s3_bucket.prompts.bucket == "acme-blog-production-prompts"
    )
    error_message = "production: deletion protection and recovery on the table, no force_destroy on the bucket"
  }
}

run "with_no_publisher_the_bucket_has_no_policy" {
  command = plan

  assert {
    condition     = length(aws_s3_bucket_policy.prompts) == 0 && length(data.aws_iam_policy_document.bucket) == 0
    error_message = "no publisher (the default): no bucket policy, and nothing is published"
  }
}

run "with_a_publisher_no_one_else_may_write_and_nothing_goes_over_plain_http" {
  command = plan

  variables {
    publisher_role_arn = "arn:aws:iam::111111111111:role/gha-acme-blog-dev-prompts"
  }

  assert {
    condition     = length(aws_s3_bucket_policy.prompts) == 1
    error_message = "a publisher named: the bucket gets its policy"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.bucket[0].statement : statement.actions if statement.sid == "OnlyThePublisherWrites"
      ])) == toset([
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObjectTagging",
      "s3:PutObjectVersionTagging",
      "s3:DeleteObjectTagging",
      "s3:DeleteObjectVersionTagging",
    ])
    error_message = "every write to an object is refused to anyone else: its bytes, its tags, which version is current"
  }

  assert {
    condition = flatten([
      for statement in data.aws_iam_policy_document.bucket[0].statement : [
        for condition in statement.condition : "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ] if statement.sid == "OnlyThePublisherWrites"
    ]) == ["ArnNotEquals aws:PrincipalArn arn:aws:iam::111111111111:role/gha-acme-blog-dev-prompts"]
    error_message = "the publisher is excepted by one condition on aws:PrincipalArn, with its ARN exactly"
  }

  assert {
    condition = alltrue(flatten([
      for statement in data.aws_iam_policy_document.bucket[0].statement : concat(
        [statement.effect == "Deny", try(length(statement.not_principals), 0) == 0],
        [for principal in statement.principals : principal.type == "*" && principal.identifiers == toset(["*"])],
      )
    ]))
    error_message = "only Deny statements, on every principal, and no Principal or NotPrincipal names the role: S3 refuses a policy naming a role that does not exist yet, and every deploy would fail"
  }

  assert {
    condition = flatten([
      for statement in data.aws_iam_policy_document.bucket[0].statement : [
        for condition in statement.condition : "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ] if statement.sid == "DenyInsecureTransport"
    ]) == ["Bool aws:SecureTransport false"]
    error_message = "TLS only: a request that is not over TLS is refused"
  }

  assert {
    condition = (
      toset(flatten([for statement in data.aws_iam_policy_document.bucket[0].statement : statement.actions if statement.sid == "DenyInsecureTransport"])) == toset(["s3:*"])
      && toset(flatten([for statement in data.aws_iam_policy_document.bucket[0].statement : statement.resources if statement.sid == "DenyInsecureTransport"])) == toset(["arn:aws:s3:::acme-blog-dev-prompts", "arn:aws:s3:::acme-blog-dev-prompts/*"])
    )
    error_message = "for every action, on the bucket and every object"
  }
}

run "a_publisher_arn_that_is_not_a_role_is_refused" {
  command = plan

  variables {
    publisher_role_arn = "arn:aws:iam::111111111111:user/someone"
  }

  expect_failures = [var.publisher_role_arn]
}

run "lambda_exec_reads_the_bucket_and_the_table_and_writes_nothing" {
  command = plan

  variables {
    publisher_role_arn = "arn:aws:iam::111111111111:role/gha-acme-blog-dev-prompts"
  }

  assert {
    condition     = aws_iam_role_policy.lambda_read.role == "acme-blog-dev-lambda-exec"
    error_message = "the policy is on the shared Lambda role the root passes"
  }

  assert {
    condition = toset(flatten(data.aws_iam_policy_document.lambda_read.statement[*].actions)) == toset([
      "s3:GetObject",
      "s3:GetObjectTagging",
      "s3:ListBucket",
      "dynamodb:GetItem",
    ])
    error_message = "reads only"
  }

  assert {
    condition = length([
      for action in flatten(data.aws_iam_policy_document.lambda_read.statement[*].actions) : action
      if can(regex("(?i)put|delete|update|write|create|\\*", action))
    ]) == 0
    error_message = "no Put, Delete, Update or wildcard anywhere"
  }

  assert {
    condition     = alltrue([for statement in data.aws_iam_policy_document.lambda_read.statement : statement.effect == "Allow" && try(length(statement.not_actions), 0) == 0 && try(length(statement.not_resources), 0) == 0])
    error_message = "plain Allow statements, nothing granted by exclusion"
  }

  assert {
    condition = [
      for statement in data.aws_iam_policy_document.lambda_read.statement : statement.actions
      if contains(statement.resources, "arn:aws:dynamodb:ap-southeast-2:111111111111:table/acme-blog-dev-prompt-hashes")
    ] == [toset(["dynamodb:GetItem"])]
    error_message = "the hash table: GetItem, in one statement, and nothing else"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.lambda_read.statement : statement.resources if contains(statement.actions, "dynamodb:GetItem")
    ])) == toset(["arn:aws:dynamodb:ap-southeast-2:111111111111:table/acme-blog-dev-prompt-hashes"])
    error_message = "GetItem on the hash table only"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.lambda_read.statement : statement.resources if contains(statement.actions, "s3:GetObjectTagging")
      ])) == toset([
      "arn:aws:s3:::acme-blog-dev-prompts/*/latest/*",
      "arn:aws:s3:::acme-blog-dev-prompts/*/versions/*",
    ])
    error_message = "a file's tags are read under latest/ and versions/ only"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.lambda_read.statement : statement.resources if contains(statement.actions, "s3:GetObject")
      ])) == toset([
      "arn:aws:s3:::acme-blog-dev-prompts/*/latest/*",
      "arn:aws:s3:::acme-blog-dev-prompts/*/versions/*",
      "arn:aws:s3:::acme-blog-dev-prompts/releases/*",
    ])
    error_message = "objects are read under latest/, versions/ and (the admin API's GET /prompts/releases) releases/"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.lambda_read.statement : statement.resources if contains(statement.actions, "s3:ListBucket")
    ])) == toset(["arn:aws:s3:::acme-blog-dev-prompts"])
    error_message = "ListBucket on the bucket itself"
  }
}
