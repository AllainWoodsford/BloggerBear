variable "environment_name" {
  type        = string
  description = "Short environment name (e.g. \"dev\", \"production\"), used to name the SNS topic/alarms/dashboard (bloggerbear-<environment_name>-...)."
}

variable "lambda_function_names" {
  type        = list(string)
  description = "Names of every pipeline Lambda function to monitor. Gets one Errors alarm and one Throttles alarm per entry, plus one dashboard widget per entry."
}

variable "state_machine_arn" {
  type        = string
  description = "ARN of the daily-cycle Step Functions state machine -- alarmed on ExecutionsFailed and charted on the dashboard."
}

variable "dlq_queue_name" {
  type        = string
  description = "Name of the daily-cycle pipeline's dead-letter queue -- alarmed on ApproximateNumberOfMessagesVisible > 0 (a failed execution is sitting there un-investigated)."
}

variable "alert_email" {
  type        = string
  default     = ""
  description = <<-EOT
    Email address to subscribe to the alerts SNS topic. Left empty by
    default -- every alarm below is created and will still publish to the
    topic either way; this only controls whether a human is actually
    notified. AWS SNS requires manually confirming the subscription (a
    confirmation email/link) before it goes active -- set this in
    terraform.tfvars and confirm the subscription once applied.
  EOT
}
