variable "prefix" {
  type        = string
  description = "Short name for every resource. Lowercase letters, digits and hyphens."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.prefix))
    error_message = "prefix must be 3 to 24 characters: lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "databricks_account_id" {
  type        = string
  description = "Databricks account ID (a GUID), from the account console. Not a secret."
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.databricks_account_id))
    error_message = "databricks_account_id must be a GUID."
  }
}

variable "region" {
  type        = string
  description = "AWS region for the workspace, for example us-east-1."
}

variable "network" {
  type        = string
  default     = "managed"
  description = "managed: Databricks creates the VPC in your account. vpc: this module creates a VPC with a NAT gateway and endpoints, and registers it."
  validation {
    condition     = contains(["managed", "vpc"], var.network)
    error_message = "network must be managed or vpc."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.4.0.0/16"
}

variable "force_destroy" {
  type        = bool
  default     = false
  description = "Let destroy delete the root bucket even when it holds workspace files. Off by default: destroy stops instead of deleting data."
}

variable "owner" {
  type        = string
  default     = "labagent"
  description = "Value of the Owner tag. Use a team or a role, not a personal email."
}

variable "tags" {
  type    = map(string)
  default = {}
}
