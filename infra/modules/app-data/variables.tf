variable "environment_name" {
  type        = string
  description = "Short environment name (e.g. \"dev\", \"production\"), used to name the DynamoDB tables (bloggerbear-<environment_name>-<table>)."
}
