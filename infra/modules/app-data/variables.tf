variable "environment_name" {
  type        = string
  description = "Short environment name (e.g. \"dev\", \"production\"), used to name the DynamoDB tables (bloggerbear-<environment_name>-<table>)."
}

variable "protect_data" {
  type        = bool
  default     = false
  description = "Turns on deletion protection and point-in-time recovery for every table. Production sets this true. It costs a little more storage (the recovery history) and means a table has to be unprotected on purpose before it can be destroyed, which is the point."
}
