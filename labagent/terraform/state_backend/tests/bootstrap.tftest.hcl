mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  bucket_name           = "example-state-bucket"
  admin_principal_arns  = ["arn:aws:iam::123456789012:role/admin"]
  runner_principal_arns = ["arn:aws:iam::123456789012:role/runner"]
}

run "bucket_is_private_versioned_and_encrypted" {
  command = plan

  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "versioning must be enabled"
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.state.block_public_acls && aws_s3_bucket_public_access_block.state.restrict_public_buckets
    error_message = "public access must be blocked"
  }
  assert {
    condition     = aws_kms_key.state.enable_key_rotation
    error_message = "KMS rotation must be on"
  }
}

run "refuses_empty_admin_list" {
  command = plan
  variables { admin_principal_arns = [] }
  expect_failures = [var.admin_principal_arns]
}
