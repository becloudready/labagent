terraform {
  # use_lockfile (S3 native state locking) is stable from 1.11. OpenTofu >= 1.10 also works.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40, < 7.0"
    }
  }

  # Partial configuration. Bucket, key, region and KMS key are supplied at `init` time with
  # -backend-config (see labagent/tf.py), so nothing organisation-specific is committed.
  backend "s3" {}
}
