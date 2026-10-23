variable "name" {
  type        = string
  description = "Name prefix for this environment's resources, e.g. \"<prefix>-dev\"."
}

variable "region" {
  type        = string
  default     = "ap-northeast-1"
  description = "Region for the gateway. The Web Search Tool connector is only offered in us-east-1, eu-west-1 and ap-northeast-1 (Tokyo, the closest to Sydney)."

  validation {
    # AWS offers the Web Search Tool in these three Regions only.
    condition     = contains(["us-east-1", "eu-west-1", "ap-northeast-1"], var.region)
    error_message = "region must be one of the Web Search Tool's regions: us-east-1, eu-west-1 or ap-northeast-1."
  }
}

variable "target_name" {
  type        = string
  default     = "web-search"
  description = "Gateway target name. The MCP tool the app calls is \"<target_name>___WebSearch\"."
}
