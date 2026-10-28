# Azure Databricks workspace on the premium SKU. The SKU is a literal on purpose: this module exists to create premium workspaces,
# and a variable would let a typo create a standard one.

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  # azurerm 5 registers no resource providers by default. Microsoft.Databricks must be registered on the subscription.
  resource_providers_to_register = ["Microsoft.Databricks"]
}

resource "random_id" "suffix" {
  byte_length = 2
}

locals {
  name = "${var.prefix}-${random_id.suffix.hex}"
  tags = merge(var.tags, { Owner = var.owner, ManagedBy = "labagent", Instance = var.prefix })
  vnet = var.network == "vnet"
}

resource "azurerm_resource_group" "this" {
  name     = "${local.name}-rg"
  location = var.location
  tags     = local.tags
}

resource "azurerm_virtual_network" "this" {
  count               = local.vnet ? 1 : 0
  name                = "${local.name}-vnet"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  address_space       = [var.vnet_cidr]
  tags                = local.tags
}

resource "azurerm_network_security_group" "this" {
  count               = local.vnet ? 1 : 0
  name                = "${local.name}-nsg"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.tags
}

resource "azurerm_subnet" "public" {
  count                = local.vnet ? 1 : 0
  name                 = "${local.name}-public"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 3, 0)]
  delegation {
    name = "databricks"
    service_delegation {
      name = "Microsoft.Databricks/workspaces"
      # No actions list: Azure adds the right defaults for this delegation (azurerm docs), and the sources disagree on the exact names.
    }
  }
}

resource "azurerm_subnet" "private" {
  count                = local.vnet ? 1 : 0
  name                 = "${local.name}-private"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 3, 1)]
  delegation {
    name = "databricks"
    service_delegation {
      name = "Microsoft.Databricks/workspaces"
      # No actions list: Azure adds the right defaults for this delegation (azurerm docs), and the sources disagree on the exact names.
    }
  }
}

resource "azurerm_subnet_network_security_group_association" "public" {
  count                     = local.vnet ? 1 : 0
  subnet_id                 = azurerm_subnet.public[0].id
  network_security_group_id = azurerm_network_security_group.this[0].id
}

resource "azurerm_subnet_network_security_group_association" "private" {
  count                     = local.vnet ? 1 : 0
  subnet_id                 = azurerm_subnet.private[0].id
  network_security_group_id = azurerm_network_security_group.this[0].id
}

resource "azurerm_databricks_workspace" "this" {
  name                        = "${local.name}-workspace"
  resource_group_name         = azurerm_resource_group.this.name
  location                    = azurerm_resource_group.this.location
  sku                         = "premium"
  managed_resource_group_name = "${local.name}-managed-rg"
  tags                        = local.tags

  # Premium-only settings (the provider rejects them on other SKUs).
  infrastructure_encryption_enabled = var.hardened

  dynamic "enhanced_security_compliance" {
    for_each = var.hardened ? [1] : []
    content {
      automatic_cluster_update_enabled = true
    }
  }

  # Secure cluster connectivity (no public IPs on clusters) is the provider default; stated here so it is visible in review.
  custom_parameters {
    no_public_ip                                         = true
    virtual_network_id                                   = local.vnet ? azurerm_virtual_network.this[0].id : null
    public_subnet_name                                   = local.vnet ? azurerm_subnet.public[0].name : null
    private_subnet_name                                  = local.vnet ? azurerm_subnet.private[0].name : null
    public_subnet_network_security_group_association_id  = local.vnet ? azurerm_subnet_network_security_group_association.public[0].id : null
    private_subnet_network_security_group_association_id = local.vnet ? azurerm_subnet_network_security_group_association.private[0].id : null
  }

  depends_on = [
    azurerm_subnet_network_security_group_association.public,
    azurerm_subnet_network_security_group_association.private,
  ]
}
