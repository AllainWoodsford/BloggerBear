variable "unique_name_prefix" {
  type        = string
  description = "What every resource name starts with, without a trailing hyphen (the calling root's var.unique_name_prefix)."
}

variable "environment_name" {
  type        = string
  description = "\"dev\" or \"production\": part of every resource's name."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,31}$", var.environment_name))
    error_message = "environment_name must be a lowercase letter followed by 1 to 31 lowercase letters or digits."
  }
}

variable "region" {
  type        = string
  description = "Where the worker runs. us-west-2 puts it beside the Sentinel-2 COGs (sentinel-cogs), so its range reads stay in one Region and only a few KB of metrics and one small PNG cross back to the home Region. The home Region also works: the reads then cross instead. The deploy role needs Lambda and log-group rights here (infra/bootstrap's vision_region)."

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.region))
    error_message = "region must look like an AWS region, e.g. us-west-2."
  }
}

variable "memory_size" {
  type        = number
  default     = 2048
  description = "MB. On Lambda, memory also sets the CPU share; OpenCV is single-threaded per call here, so past ~1.8 GB (one full vCPU) more memory buys little. The benchmark (docs §4) is what should move this."
}

variable "timeout" {
  type        = number
  default     = 60
  description = "Seconds, at most 60. A site is read in a few seconds; this is the ceiling for a slow read. It is under common/vision_client.py's default 90 s read timeout, but the research tick's adapters (rail_access and satellite_vision) wait only 58 s (WORKER_READ_TIMEOUT in common/vision_sites.py), so their client gives up first, by design: the tick must keep time to finish, and a scene left unmeasured is tried again next tick."
}
