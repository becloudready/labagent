output "workspace_url" {
  description = "Host name of the workspace, without https://."
  value       = azurerm_databricks_workspace.this.workspace_url
}

output "workspace_id" {
  value = azurerm_databricks_workspace.this.workspace_id
}

output "sku" {
  description = "Always premium. The pack checks this after the apply."
  value       = azurerm_databricks_workspace.this.sku
}

output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "managed_resource_group_id" {
  value = azurerm_databricks_workspace.this.managed_resource_group_id
}

output "network_mode" {
  value = var.network
}
