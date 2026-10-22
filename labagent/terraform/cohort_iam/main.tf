###############################################################################
# One cohort's IAM layer: learner users, a sandbox policy + group, and one
# shared Lambda execution role. Everything is cohort-scoped, so `destroy`
# removes exactly this cohort and nothing else.
#
# Deliberately NOT managed here: console passwords (aws_iam_user_login_profile).
# A login profile stores the password in state. labagent issues passwords with the
# IAM API and returns them only as ciphertext, so state holds no secrets.
###############################################################################

provider "aws" {
  region = var.region

  default_tags {
    tags = merge(var.extra_tags, {
      workshop   = var.workshop
      cohort     = var.cohort
      date       = var.created_date
      autodelete = "false" # IAM is removed by `destroy`, never by the resource sweeper
    })
  }
}

data "aws_caller_identity" "current" {}

locals {
  group_name       = "${var.name_prefix}-${var.cohort}-students"
  policy_name      = "${var.name_prefix}-${var.cohort}-sandbox"
  lambda_role_name = "${var.name_prefix}-${var.cohort}-lambda-exec"
}

resource "aws_iam_user" "student" {
  for_each = var.learners

  name          = each.value.username
  force_destroy = true # also removes login profile, access keys and MFA devices created outside Terraform

  tags = {
    learner_id = each.key
  }
}

resource "aws_iam_policy" "sandbox" {
  name        = local.policy_name
  description = "Region-locked sandbox for cohort ${var.cohort}."

  policy = templatefile("${path.module}/sandbox-policy.json.tftpl", {
    region           = var.region
    account_id       = data.aws_caller_identity.current.account_id
    lambda_role_name = local.lambda_role_name
  })
}

resource "aws_iam_group" "students" {
  name = local.group_name
}

resource "aws_iam_group_policy_attachment" "sandbox" {
  group      = aws_iam_group.students.name
  policy_arn = aws_iam_policy.sandbox.arn
}

resource "aws_iam_user_group_membership" "student" {
  for_each = aws_iam_user.student

  user   = each.value.name
  groups = [aws_iam_group.students.name]
}

# Shared Lambda execution role: students need no IAM permissions of their own.
resource "aws_iam_role" "lambda_exec" {
  name = local.lambda_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_dynamodb" {
  name = "${var.name_prefix}-${var.cohort}-lambda-dynamodb"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:*"
      Resource = "*"
    }]
  })
}
