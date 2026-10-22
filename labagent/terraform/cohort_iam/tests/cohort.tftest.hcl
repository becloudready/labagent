# Plan-level tests with a mocked AWS provider: no credentials, no cost.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  cohort       = "demo-1"
  created_date = "01-Jan-2030"
  learners = {
    "1" = { username = "ada@example.com" }
    "2" = { username = "alan@example.com" }
  }
}

run "creates_one_user_per_learner_with_cohort_scoped_names" {
  command = plan

  assert {
    condition     = length(aws_iam_user.student) == 2
    error_message = "expected one IAM user per learner"
  }
  assert {
    condition     = aws_iam_group.students.name == "labs-demo-1-students"
    error_message = "group name must be cohort scoped"
  }
  assert {
    condition     = aws_iam_role.lambda_exec.name == "labs-demo-1-lambda-exec"
    error_message = "role name must be cohort scoped"
  }
}

run "policy_is_region_locked_and_scopes_passrole_to_the_cohort_role" {
  command = plan

  assert {
    condition     = strcontains(aws_iam_policy.sandbox.policy, "\"aws:RequestedRegion\": \"us-east-1\"")
    error_message = "region lock missing"
  }
  assert {
    condition     = strcontains(aws_iam_policy.sandbox.policy, "role/labs-demo-1-lambda-exec")
    error_message = "PassRole must be limited to the cohort Lambda role"
  }
  assert {
    condition     = strcontains(aws_iam_policy.sandbox.policy, "DenyLargeInstances")
    error_message = "instance size guard missing"
  }
}

run "rejects_bad_cohort_id" {
  command = plan
  variables { cohort = "Bad Cohort!" }
  expect_failures = [var.cohort]
}

run "rejects_bad_username" {
  command = plan
  variables {
    learners = { "1" = { username = "has spaces@example.com" } }
  }
  expect_failures = [var.learners]
}
