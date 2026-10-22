variable "region" {
  description = "The one region students are locked to."
  type        = string
  default     = "us-east-1"
}

variable "cohort" {
  description = "Cohort id. Used in resource names and the `cohort` tag."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,39}$", var.cohort))
    error_message = "cohort must be 3 to 40 chars: lowercase letters, digits and hyphens."
  }
}

variable "created_date" {
  description = "Batch date for the `date` tag, dd-mmm-yyyy (for example 12-Jul-2026)."
  type        = string
}

variable "workshop" {
  description = "Value of the `workshop` tag that marks lab resources."
  type        = string
  default     = "workshop"
}

variable "name_prefix" {
  description = "Prefix for the cohort's IAM group, policy and Lambda role."
  type        = string
  default     = "labs"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be 2 to 21 chars, start with a letter, letters/digits/hyphens only."
  }
}

variable "learners" {
  description = "Learners keyed by learner_id. username is the IAM user name (an email-style identifier)."
  type = map(object({
    username = string
  }))

  validation {
    condition     = alltrue([for k, v in var.learners : can(regex("^[\\w+=,.@-]{1,64}$", v.username))])
    error_message = "every username must be 1 to 64 chars of letters, digits and + = , . @ _ -"
  }
}

variable "extra_tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
