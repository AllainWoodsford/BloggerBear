variable "unique_name_prefix" {
  type        = string
  description = "What every resource name starts with, without a trailing hyphen (the calling root's var.unique_name_prefix). The bucket is <prefix>-<environment_name>-prompts and the table <prefix>-<environment_name>-prompt-hashes."
}

variable "environment_name" {
  type        = string
  description = "\"dev\" or \"production\": part of every resource's name."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,31}$", var.environment_name))
    error_message = "environment_name must be a lowercase letter followed by 1 to 31 lowercase letters or digits."
  }
}

variable "protect_data" {
  type        = bool
  default     = false
  description = "Production sets this true: the hash table gets deletion protection and point-in-time recovery, and the bucket cannot be destroyed while it holds a prompt. Off in dev, where both are disposable and the publisher fills them again from the repository."
}

variable "publisher_role_arn" {
  type        = string
  default     = ""
  description = "The IAM role the prompts publisher signs in as (the root's var.prompts_publisher_role_arn): the one principal the bucket policy lets write, tag or delete an object. Empty: no publisher, so the bucket gets no policy and nothing is published. The role need not exist yet: the policy names it in a condition, which S3 accepts either way."

  validation {
    condition     = var.publisher_role_arn == "" || can(regex("^arn:aws:iam::[0-9]{12}:role/[\\w+=,.@/-]{1,128}$", var.publisher_role_arn))
    error_message = "publisher_role_arn must be empty or an IAM role ARN: arn:aws:iam::<account id>:role/<name>."
  }
}

variable "lambda_exec_role_id" {
  type        = string
  description = "The shared Lambda execution role's name. The roots pass aws_iam_role_policy.lambda_backup.role (the same value as aws_iam_role.lambda_exec.id), so this module's policy is created after that one. It gets one inline policy here that reads the bucket and the hash table and writes nothing."
}
