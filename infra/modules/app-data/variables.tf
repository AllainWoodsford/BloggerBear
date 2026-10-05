variable "unique_name_prefix" {
  type        = string
  description = "What every resource name starts with, without a trailing hyphen (the calling root's var.unique_name_prefix, \"bloggerbear\" by default). Every table is <prefix>-<environment_name>-<table>."
}

variable "environment_name" {
  type        = string
  description = "Short environment name (e.g. \"dev\", \"production\"), used to name the DynamoDB tables (<prefix>-<environment_name>-<table>)."
}

variable "protect_data" {
  type        = bool
  default     = false
  description = "Turns on deletion protection and point-in-time recovery for every table. Production sets this true. It costs a little more storage (the recovery history) and means a table has to be unprotected on purpose before it can be destroyed, which is the point."
}
