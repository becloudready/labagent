# Plan-level tests with mocked providers: no credentials, no cost.
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }
}
mock_provider "databricks" {
  mock_data "databricks_aws_assume_role_policy" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "databricks_aws_crossaccount_policy" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "databricks_aws_bucket_policy" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}
mock_provider "random" {}
mock_provider "time" {}

variables {
  prefix                = "demo"
  databricks_account_id = "00000000-0000-0000-0000-000000000000"
  region                = "us-east-1"
}

run "managed_network_registers_no_vpc" {
  command = plan
  assert {
    condition     = length(databricks_mws_networks.this) == 0 && length(module.vpc) == 0
    error_message = "network=managed must not create or register a VPC"
  }
  assert {
    condition     = databricks_mws_workspaces.this.aws_region == "us-east-1"
    error_message = "workspace must be in the requested region"
  }
}

run "vpc_mode_creates_and_registers_a_vpc" {
  command = plan
  variables {
    network = "vpc"
  }
  assert {
    condition     = length(databricks_mws_networks.this) == 1 && length(module.vpc) == 1 && length(module.vpc_endpoints) == 1
    error_message = "network=vpc must create the VPC, its endpoints and register the network"
  }
}

run "root_bucket_is_private_and_not_force_destroyed_by_default" {
  command = plan
  assert {
    condition     = aws_s3_bucket.root.force_destroy == false
    error_message = "destroy must not delete workspace files unless asked"
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.root.block_public_acls && aws_s3_bucket_public_access_block.root.block_public_policy && aws_s3_bucket_public_access_block.root.restrict_public_buckets
    error_message = "the root bucket must block public access"
  }
}

run "force_destroy_is_opt_in" {
  command = plan
  variables {
    force_destroy = true
  }
  assert {
    condition     = aws_s3_bucket.root.force_destroy == true
    error_message = "force_destroy must reach the bucket"
  }
}

run "bad_inputs_are_refused" {
  command = plan
  variables {
    databricks_account_id = "abc"
  }
  expect_failures = [var.databricks_account_id]
}
