variable "name" {
  type        = string
  description = "Name prefix for this environment's resources, e.g. \"bloggerbear-dev\"."
}

variable "region" {
  type        = string
  default     = "ap-northeast-1"
  description = "Region for the gateway. The Web Search Tool connector is only offered in us-east-1, eu-west-1 and ap-northeast-1 (Tokyo, the closest to Sydney)."
}

variable "target_name" {
  type        = string
  default     = "web-search"
  description = "Gateway target name. The MCP tool the app calls is \"<target_name>___WebSearch\"."
}
