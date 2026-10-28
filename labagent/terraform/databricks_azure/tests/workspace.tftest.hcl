# Plan-level tests with a mocked Azure provider: no credentials, no cost.
mock_provider "azurerm" {}
mock_provider "random" {}

variables {
  prefix          = "demo"
  subscription_id = "00000000-0000-0000-0000-000000000000"
  location        = "eastus2"
}

run "workspace_is_always_premium" {
  command = plan
  assert {
    condition     = azurerm_databricks_workspace.this.sku == "premium"
    error_message = "the workspace must use the premium SKU"
  }
  assert {
    condition     = azurerm_databricks_workspace.this.custom_parameters[0].no_public_ip == true
    error_message = "clusters must have no public IP"
  }
}

run "managed_network_creates_no_virtual_network" {
  command = plan
  assert {
    condition     = length(azurerm_virtual_network.this) == 0 && length(azurerm_subnet.public) == 0
    error_message = "network=managed must not create a virtual network"
  }
}

run "vnet_mode_creates_a_delegated_network" {
  command = plan
  variables {
    network = "vnet"
  }
  assert {
    condition     = length(azurerm_virtual_network.this) == 1 && length(azurerm_subnet_network_security_group_association.private) == 1
    error_message = "network=vnet must create the virtual network, two subnets and both security group associations"
  }
  assert {
    condition     = azurerm_subnet.public[0].delegation[0].service_delegation[0].name == "Microsoft.Databricks/workspaces"
    error_message = "subnets must be delegated to Azure Databricks"
  }
}

run "hardened_turns_on_the_premium_only_settings" {
  command = plan
  variables {
    hardened = true
  }
  assert {
    condition     = azurerm_databricks_workspace.this.infrastructure_encryption_enabled == true
    error_message = "hardened must enable infrastructure encryption"
  }
  assert {
    condition     = length(azurerm_databricks_workspace.this.enhanced_security_compliance) == 1
    error_message = "hardened must enable automatic cluster updates"
  }
}

run "tags_carry_owner_and_the_tool" {
  command = plan
  variables {
    owner = "training-team"
  }
  assert {
    condition     = azurerm_resource_group.this.tags["Owner"] == "training-team" && azurerm_resource_group.this.tags["ManagedBy"] == "labagent"
    error_message = "resources must be tagged with the owner and labagent"
  }
}

run "bad_names_and_ids_are_refused" {
  command = plan
  variables {
    prefix = "Bad Name"
  }
  expect_failures = [var.prefix]
}

run "bad_subscription_is_refused" {
  command = plan
  variables {
    subscription_id = "not-a-guid"
  }
  expect_failures = [var.subscription_id]
}
