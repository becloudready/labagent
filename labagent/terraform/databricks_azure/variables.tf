variable "prefix" {
  type        = string
  description = "Short name for every resource. Lowercase letters, digits and hyphens."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.prefix))
    error_message = "prefix must be 3 to 24 characters: lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "subscription_id" {
  type        = string
  description = "Azure subscription that will hold the workspace."
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a GUID."
  }
}

variable "location" {
  type        = string
  description = "Azure region, for example eastus2."
}

variable "network" {
  type        = string
  default     = "managed"
  description = "managed: Azure Databricks creates the virtual network. vnet: this module creates a virtual network and injects the workspace into it."
  validation {
    condition     = contains(["managed", "vnet"], var.network)
    error_message = "network must be managed or vnet."
  }
}

variable "vnet_cidr" {
  type        = string
  default     = "10.179.0.0/20"
  description = "Address range for the virtual network when network is vnet."
}

variable "hardened" {
  type        = bool
  default     = false
  description = "Premium-only extras: a second layer of encryption on the workspace file system, and automatic cluster updates."
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
