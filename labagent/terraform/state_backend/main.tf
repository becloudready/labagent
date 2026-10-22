###############################################################################
# One-time bootstrap: an encrypted, versioned, private S3 bucket for Terraform
# state, plus a KMS key. Run it once per AWS account with local state, then keep
# its (secret-free) state somewhere safe.
#
#   cd "$(labagent bootstrap-path)"     # the module ships inside the labagent package
#   terraform init && terraform apply \
#     -var bucket_name=<globally-unique-name> \
#     -var 'admin_principal_arns=["arn:aws:iam::<account>:role/<your-admin-role>"]' \
#     -var 'runner_principal_arns=["arn:aws:iam::<account>:role/<runner-role>"]'
#
# Nothing here is specific to any organisation. All values come from variables.
###############################################################################

terraform {
  required_version = ">= 1.11.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40, < 7.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "bucket_name" {
  description = "Globally unique bucket name for Terraform state."
  type        = string
}

variable "admin_principal_arns" {
  description = "Principals that administer the bucket (must include whoever runs this bootstrap)."
  type        = list(string)

  validation {
    condition     = length(var.admin_principal_arns) > 0
    error_message = "admin_principal_arns must not be empty: the bucket policy would lock everyone out."
  }
}

variable "runner_principal_arns" {
  description = "Roles that run labagent (read/write state). Use the OIDC-assumed runner roles."
  type        = list(string)
}

variable "noncurrent_version_days" {
  type    = number
  default = 90
}

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "state" {
  description             = "Terraform state encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "state" {
  name          = "alias/${var.bucket_name}"
  target_key_id = aws_kms_key.state.key_id
}

resource "aws_s3_bucket" "state" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_days
    }
  }
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyEveryoneExceptAdminsAndRunners"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = concat(var.admin_principal_arns, var.runner_principal_arns)
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket     = aws_s3_bucket.state.id
  policy     = data.aws_iam_policy_document.bucket.json
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# Attach this to the runner role(s): the minimum labagent needs for state + native locking.
data "aws_iam_policy_document" "runner_access" {
  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    sid       = "ReadWriteCohortState"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/cohorts/*"]
  }
  statement {
    sid       = "UseStateKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.state.arn]
  }
}

output "backend_config" {
  description = "Values for labagent's `backend` config block."
  value = {
    bucket       = aws_s3_bucket.state.id
    region       = var.region
    kms_key_id   = aws_kms_key.state.arn
    use_lockfile = true
  }
}

output "runner_access_policy_json" {
  description = "Attach to the runner role(s)."
  value       = data.aws_iam_policy_document.runner_access.json
}
