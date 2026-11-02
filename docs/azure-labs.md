# Azure Labs: move Azure VMs from one resource group to another

A simple lift and shift for Azure VMs. It copies the VMs, their disks and their network into another resource group, and it never changes the source.
It can also just snapshot disks, for an old VM or disk you want to keep somewhere else.

This is an alternative for people moving off Azure Lab Services (retiring June 28, 2027). It is not Azure Lab Services and not from Microsoft.
Lab VMs are ordinary Azure VMs, so this works for any VM in a resource group, not only lab VMs.

## What you need

- The Azure CLI, signed in: `az login`.
- Your subscription ID in the allow list. labagent refuses to run on any other subscription.

```bash
export LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS=$(az account show --query id -o tsv)
```

PowerShell: `$env:LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS = az account show --query id -o tsv`

## Copy VMs to another resource group

```bash
labagent launch azure-labs --name move1 --param source_rg=azure-labs-legacy --param target_rg=azure-labs-new
```

| Parameter | Meaning |
|---|---|
| `source_rg` | Group to copy from. It is only read. |
| `target_rg` | Group to copy into. Created if it does not exist. |
| `vms` | Comma list of VM names. Default: every VM in the source group. |
| `snapshot_only` | `true` copies only disk snapshots (no VMs, no network). With no `vms`, it also takes disks that no VM is using. |
| `subscription` | Subscription ID or name. Default: the one `az login` selected. |

What it does, in order: reads the source, checks vCPU quota, creates the target group if needed, snapshots every disk into the target,
copies the virtual network, subnets, network security groups (with their rules), public IPs and NICs, creates disks from the snapshots,
creates the VMs on those disks, and checks that each VM is provisioned. Every copied resource keeps its name and gets these tags:
`labagent=1`, `pack=azure-labs`, `name`, `migrated_from`, `created_at`.

Run it again after a failure: it skips what already exists and carries on. Add `--dry-run` to see the steps without running them.

The new VMs boot the same operating system with the same accounts and data (a specialized copy), so no password is needed or created.
They start running. The source VMs keep running too, and both count against your vCPU quota. Stop the sources, or copy fewer VMs at a time with `vms=`.

## Snapshot an old VM or disk

```bash
labagent launch azure-labs --name old1 --param source_rg=azure-labs-legacy --param target_rg=azure-labs-archive --param snapshot_only=true
```

Snapshots land in the target group as `snap-<disk name>`. Create a disk or VM from one whenever you need it.

## Delete

```bash
labagent destroy azure-labs --name move1
```

Deletes only what that migration created in the target group (found by its tags), dependants first. It deletes the target group too if labagent created it
and nothing else is in it. Anything in the group without the tags is reported and left alone. The source group is never touched.

## What it does not copy

It stops before creating anything, and lists the reasons, if a VM is in an availability set or scale set, uses an unmanaged VHD,
uses encrypted disks with your own keys, has a NIC with more than one IP configuration, or uses application security groups.

- Public IP addresses change: you get a new address with the same name.
- Data disk order is kept, but LUN numbers and caching settings are not.
- VM extensions, managed identities, boot diagnostics settings and backups are not copied.
- Same subscription and same region only. Copying across subscriptions is not built.
- Managed images are not copied. Create a VM from the image, then snapshot that.

## Try it on a test group

Create a small legacy group first. Three B1s VMs fit a 10-vCPU quota twice over.

```bash
az group create -n azure-labs-legacy -l canadacentral
az network nsg create -g azure-labs-legacy -n legacy-nsg
az network nsg rule create -g azure-labs-legacy --nsg-name legacy-nsg -n allow-ssh --priority 100 --destination-port-ranges 22 --access Allow --protocol Tcp
az network vnet create -g azure-labs-legacy -n legacy-vnet --address-prefixes 10.30.0.0/16 --subnet-name app --subnet-prefixes 10.30.1.0/24 --network-security-group legacy-nsg
for i in 1 2 3; do
  az vm create -g azure-labs-legacy -n legacy-vm$i --image Ubuntu2204 --size Standard_B1s --vnet-name legacy-vnet --subnet app \
    --public-ip-address "" --nsg "" --admin-username azureuser --generate-ssh-keys
done
az vm disk attach -g azure-labs-legacy --vm-name legacy-vm1 --name legacy-vm1-data --new --size-gb 8
az disk create -g azure-labs-legacy -n old-dead-disk --size-gb 4
```

Then run the copy, check the result, and delete it:

```bash
labagent launch azure-labs --name test1 --param source_rg=azure-labs-legacy --param target_rg=azure-labs-new
az vm list -g azure-labs-new -d -o table            # 3 VMs, running
az resource list -g azure-labs-legacy -o table      # unchanged
labagent destroy azure-labs --name test1
az group exists -n azure-labs-new                   # false
```

Clean up the test source with `az group delete -n azure-labs-legacy --yes`.
