# AWS Databricks workspace. The account-level provider reads DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET from the environment,
# so no credential is ever a Terraform variable or lands in state or in a var file.
#
# Premium tier: on AWS the tier comes from the Databricks account plan, not from this module. The workspace reports it back as
# `pricing_tier`; the labagent pack checks that output after the apply.

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}

provider "databricks" {
  alias      = "mws"
  host       = "https://accounts.cloud.databricks.com"
  account_id = var.databricks_account_id # OAuth for the account API needs it at the provider too
}

resource "random_id" "suffix" {
  byte_length = 3
}

locals {
  name = "${var.prefix}-${random_id.suffix.hex}"
  tags = merge(var.tags, { Owner = var.owner, ManagedBy = "labagent", Instance = var.prefix })
  vpc  = var.network == "vpc"
}

# ---- cross-account role ----------------------------------------------------------------------------------------------------------

data "databricks_aws_assume_role_policy" "this" {
  provider    = databricks.mws
  external_id = var.databricks_account_id
}

resource "aws_iam_role" "cross_account" {
  name               = "${local.name}-crossaccount"
  assume_role_policy = data.databricks_aws_assume_role_policy.this.json
}

# "managed" lets Databricks create the VPC; "customer" is for a VPC we create. Using the wrong one fails the credentials check.
data "databricks_aws_crossaccount_policy" "this" {
  provider    = databricks.mws
  policy_type = local.vpc ? "customer" : "managed"
}

resource "aws_iam_role_policy" "cross_account" {
  name   = "${local.name}-policy"
  role   = aws_iam_role.cross_account.id
  policy = data.databricks_aws_crossaccount_policy.this.json
}

# IAM changes take a few seconds to spread; the credentials check fails without this pause. The trigger repeats the pause
# whenever the policy changes, not only on the first run.
resource "time_sleep" "iam_ready" {
  depends_on      = [aws_iam_role_policy.cross_account]
  create_duration = "30s"
  triggers        = { policy = sha256(aws_iam_role_policy.cross_account.policy) }
}

resource "databricks_mws_credentials" "this" {
  provider         = databricks.mws
  account_id       = var.databricks_account_id
  credentials_name = "${local.name}-creds"
  role_arn         = aws_iam_role.cross_account.arn
  depends_on       = [time_sleep.iam_ready]
}

# ---- root bucket -----------------------------------------------------------------------------------------------------------------

resource "aws_s3_bucket" "root" {
  bucket        = "${local.name}-rootbucket"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_server_side_encryption_configuration" "root" {
  bucket = aws_s3_bucket.root.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "root" {
  bucket                  = aws_s3_bucket.root.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "databricks_aws_bucket_policy" "this" {
  provider = databricks.mws
  bucket   = aws_s3_bucket.root.bucket
}

resource "aws_s3_bucket_policy" "root" {
  bucket     = aws_s3_bucket.root.id
  policy     = data.databricks_aws_bucket_policy.this.json
  depends_on = [aws_s3_bucket_public_access_block.root]
}

resource "databricks_mws_storage_configurations" "this" {
  provider                   = databricks.mws
  account_id                 = var.databricks_account_id
  storage_configuration_name = "${local.name}-storage"
  bucket_name                = aws_s3_bucket.root.bucket
  depends_on                 = [aws_s3_bucket_policy.root]
}

# ---- optional customer-managed VPC -----------------------------------------------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

module "vpc" {
  count   = local.vpc ? 1 : 0
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = local.name
  cidr = var.vpc_cidr
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)

  public_subnets  = [cidrsubnet(var.vpc_cidr, 4, 0)]
  private_subnets = [cidrsubnet(var.vpc_cidr, 4, 1), cidrsubnet(var.vpc_cidr, 4, 2)]

  enable_dns_hostnames = true
  enable_nat_gateway   = true
  single_nat_gateway   = true

  manage_default_security_group  = true
  default_security_group_name    = "${local.name}-sg"
  default_security_group_egress  = [{ cidr_blocks = "0.0.0.0/0" }]
  default_security_group_ingress = [{ description = "All traffic inside the security group", self = true }]
}

module "vpc_endpoints" {
  count   = local.vpc ? 1 : 0
  source  = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints"
  version = "~> 6.0"

  vpc_id             = module.vpc[0].vpc_id
  security_group_ids = [module.vpc[0].default_security_group_id]

  endpoints = {
    s3 = {
      service         = "s3"
      service_type    = "Gateway"
      route_table_ids = concat(module.vpc[0].private_route_table_ids, module.vpc[0].public_route_table_ids)
      tags            = { Name = "${local.name}-s3" }
    }
    sts = {
      service             = "sts"
      private_dns_enabled = true
      subnet_ids          = module.vpc[0].private_subnets
      tags                = { Name = "${local.name}-sts" }
    }
    kinesis-streams = {
      service             = "kinesis-streams"
      private_dns_enabled = true
      subnet_ids          = module.vpc[0].private_subnets
      tags                = { Name = "${local.name}-kinesis" }
    }
  }
}

resource "databricks_mws_networks" "this" {
  count              = local.vpc ? 1 : 0
  provider           = databricks.mws
  account_id         = var.databricks_account_id
  network_name       = "${local.name}-network"
  vpc_id             = module.vpc[0].vpc_id
  subnet_ids         = module.vpc[0].private_subnets
  security_group_ids = [module.vpc[0].default_security_group_id]
}

# ---- the workspace ---------------------------------------------------------------------------------------------------------------

resource "databricks_mws_workspaces" "this" {
  provider       = databricks.mws
  account_id     = var.databricks_account_id
  workspace_name = local.name
  aws_region     = var.region

  credentials_id           = databricks_mws_credentials.this.credentials_id
  storage_configuration_id = databricks_mws_storage_configurations.this.storage_configuration_id
  network_id               = local.vpc ? databricks_mws_networks.this[0].network_id : null

  custom_tags = local.tags

  depends_on = [time_sleep.iam_ready] # the role's permissions are checked while the workspace is created
}
