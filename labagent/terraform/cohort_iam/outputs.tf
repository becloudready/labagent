output "console_url" {
  value = "https://${data.aws_caller_identity.current.account_id}.signin.aws.amazon.com/console"
}

output "region" {
  value = var.region
}

output "group_name" {
  value = aws_iam_group.students.name
}

output "lambda_role_arn" {
  description = "Give to learners as the role to pass to their Lambda labs."
  value       = aws_iam_role.lambda_exec.arn
}

output "users" {
  description = "learner_id to IAM user name. Contains no secrets."
  value       = { for k, u in aws_iam_user.student : k => u.name }
}
