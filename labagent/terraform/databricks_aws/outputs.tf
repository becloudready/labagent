output "workspace_url" {
  description = "Workspace URL, with https://."
  value       = databricks_mws_workspaces.this.workspace_url
}

output "workspace_id" {
  value = databricks_mws_workspaces.this.workspace_id
}

output "pricing_tier" {
  description = "Reported by Databricks. The pack requires PREMIUM or ENTERPRISE."
  value       = databricks_mws_workspaces.this.pricing_tier
}

output "workspace_status" {
  value = databricks_mws_workspaces.this.workspace_status
}

output "root_bucket" {
  value = aws_s3_bucket.root.bucket
}

output "network_mode" {
  value = var.network
}
