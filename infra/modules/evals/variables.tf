variable "unique_name_prefix" {
  type        = string
  description = "What every resource name starts with, without a trailing hyphen (the calling root's var.unique_name_prefix)."
}

variable "environment_name" {
  type        = string
  description = "\"dev\" or \"production\": part of every resource's name, and of the metrics' namespace."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,31}$", var.environment_name))
    error_message = "environment_name must be a lowercase letter followed by 1 to 31 lowercase letters or digits."
  }
}

variable "aws_region" {
  type        = string
  description = "The deployment's home region (the root's var.aws_region). No default, so a root that forgets it fails validate. eval-work's ARN is built with it, for the grant that holds while the queue does not exist."
}

variable "enabled" {
  type        = bool
  description = "Whether evals run (the root's var.evals_enabled, from the EVALS_ENABLED GitHub variable). Off: the table, the log group and lambda_exec's rights only. On: the eval-work queue, the eval-jobs function with its schedules and queue mapping, the metric filters and the alarms as well."
}

variable "protect_data" {
  type        = bool
  default     = false
  description = "Deletion protection and point-in-time recovery for the evals table, like the app tables (modules/app-data). Production sets this true."
}

variable "lambda_exec_role_arn" {
  type        = string
  description = "The shared Lambda execution role's ARN (the root's aws_iam_role.lambda_exec): eval-jobs runs as it, like every pipeline Lambda."
}

variable "lambda_exec_role_name" {
  type        = string
  description = "The same role's name: this module adds its inline policy to it (the log group, the table, eval-work and the alerts topic). The roots pass aws_iam_role_policy.lambda_backup.role, the same name, so the policy is written after that one."
}

variable "lambda_package_path" {
  type        = string
  description = "The shared Lambda package's zip (the root's data.archive_file.lambdas output_path), which holds eval_jobs_handler.py."
}

variable "lambda_package_hash" {
  type        = string
  description = "That zip's base64 SHA-256 (output_base64sha256), so a new package redeploys eval-jobs."
}

variable "lambda_environment" {
  type        = map(string)
  description = "The environment every pipeline Lambda has (the root's local.lambda_env_variables). The module sets EVALS_TABLE, EVALS_LOG_GROUP and EVAL_WORK_QUEUE_URL on top for eval-jobs."
}

variable "pipeline_dlq_arn" {
  type        = string
  description = "The pipeline's dead-letter queue (the root's aws_sqs_queue.pipeline_dlq): eval-work redrives a job there after three receives, and dlq_handler records it in failed-executions."
}

variable "alerts_topic_arn" {
  type        = string
  description = "The environment's alerts topic (module.observability's sns_topic_arn): every alarm here notifies it, and lambda_exec may publish to it (the evals digest)."
}

variable "prompt_fallback_metric" {
  type = object({
    namespace = string
    name      = string
  })
  description = "The metric the observability module's prompt_fallback filters feed (module.observability's prompt_fallback_metric), for the ranch-and-prompts dashboard's fallbacks panel."
}
