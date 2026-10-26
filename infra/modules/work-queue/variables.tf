variable "unique_name_prefix" {
  type        = string
  description = "What every resource name starts with, without a trailing hyphen (the calling root's var.unique_name_prefix)."
}

variable "environment_name" {
  type        = string
  description = "\"dev\" or \"production\": part of every resource's name, and of the heartbeat metric's namespace."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,31}$", var.environment_name))
    error_message = "environment_name must be a lowercase letter followed by 1 to 31 lowercase letters or digits."
  }
}

variable "lambda_exec_role_arn" {
  type        = string
  description = "The shared Lambda execution role's ARN (the root's aws_iam_role.lambda_exec): the dispatcher runs as it, like every pipeline Lambda."
}

variable "lambda_exec_role_name" {
  type        = string
  description = "The same role's name: this module adds its two inline policies to it (sending and starting, and consuming the two queues)."
}

variable "lambda_package_path" {
  type        = string
  description = "The shared Lambda package's zip (the root's data.archive_file.lambdas output_path), which holds dispatcher_handler.py."
}

variable "lambda_package_hash" {
  type        = string
  description = "That zip's base64 SHA-256 (output_base64sha256), so a new package redeploys the dispatcher."
}

variable "lambda_environment" {
  type        = map(string)
  description = "The environment every pipeline Lambda has (the root's local.lambda_env_variables): the tables the dispatcher reads and writes, the prefix and the environment name. The module sets WORK_QUEUE_URL and STATE_MACHINE_ARN on top."
}

variable "research_tick_arn" {
  type        = string
  description = "The research tick's ARN: the work queue's event source mapping invokes it with one research job at a time."
}

variable "dlq_handler_arn" {
  type        = string
  description = "dlq_handler's ARN: the dead-letter queue's event source mapping invokes it, and it records each failed job in failed-executions."
}

variable "state_machine_arn" {
  type        = string
  description = "The daily-cycle state machine's ARN: the dispatcher starts it for a topic on the queue, and in shadow mode lists and describes its executions."

  validation {
    condition     = strcontains(var.state_machine_arn, ":stateMachine:")
    error_message = "state_machine_arn must be a state machine's ARN (arn:aws:states:<region>:<account>:stateMachine:<name>): its executions' ARN is made from it."
  }
}

variable "worker_concurrency" {
  type        = number
  description = "The most research jobs that run at once: the work queue's event source mapping's maximum_concurrency (the root's var.worker_concurrency, from the WORKER_CONCURRENCY GitHub variable). At least 2, the smallest value SQS accepts."

  validation {
    condition     = var.worker_concurrency >= 2 && floor(var.worker_concurrency) == var.worker_concurrency
    error_message = "worker_concurrency must be a whole number, at least 2: the smallest maximum_concurrency an SQS event source mapping accepts."
  }
}

variable "alerts_topic_arn" {
  type        = string
  description = "The environment's alerts topic (module.observability's sns_topic_arn): every alarm here notifies it."
}
