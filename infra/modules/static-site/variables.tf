variable "environment_name" {
  type        = string
  description = "Short environment name (e.g. \"dev\", \"production\"), used to name resources."
}

variable "enable_custom_domain" {
  type        = bool
  description = "When false, the distribution only ever uses its default *.cloudfront.net domain and no ACM/Route53 resources are created. When true, domain_name and hosted_zone_id are required."
}

variable "force_destroy" {
  type        = bool
  default     = false
  description = "Whether the site S3 bucket can be destroyed even when non-empty. Dev sets this true so `terraform destroy` never chokes on a non-empty bucket; production leaves it false so an accidental destroy can't silently delete real content."
}

variable "domain_name" {
  type        = string
  default     = ""
  description = "Custom domain for the CloudFront distribution. Only used when enable_custom_domain = true."
}

variable "hosted_zone_id" {
  type        = string
  default     = ""
  description = "Route 53 hosted zone ID to create the ACM validation and alias records in. Only used when enable_custom_domain = true."
}

variable "redirect_www" {
  type        = bool
  default     = false
  description = "Only used when enable_custom_domain = true. Also answers on www.<domain_name> and 301-redirects it to <domain_name> (same path and query string). Adds a SAN to the certificate, a second CloudFront alias, www A/AAAA records and a small CloudFront Function. Without it, www.<domain_name> does not exist at all, so a visitor who types www gets an error."
}

variable "web_acl_id" {
  type        = string
  default     = ""
  description = "ARN of a pre-existing, shared WAFv2 Web ACL (CLOUDFRONT scope, created in us-east-1) to associate with this distribution. This module never creates the ACL itself -- it's created once, outside the module, and shared across environments. Leave empty to skip association."
}

variable "extra_connect_src" {
  type        = list(string)
  default     = []
  description = "Extra hostnames (no scheme) the site's scripts may call, added to the Content-Security-Policy connect-src as https://<host> -- the public API's CloudFront distribution."
}

variable "allow_microphone" {
  type        = bool
  default     = false
  description = "Whether the site's own pages may ask the browser for the microphone: Permissions-Policy microphone=(self) instead of microphone=(). Only for an environment that serves the operator's assistant (frontend/ask.html), whose push-to-talk button uses the browser's speech recognition. The browser still asks the person before any page hears anything, and no other origin (a frame, a third party) is ever allowed."
}
