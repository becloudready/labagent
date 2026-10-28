# Premium Databricks workspace on AWS or Azure

`dbx-workspace` creates a premium Databricks workspace in your own cloud account with Terraform, and removes it again. It runs **plan first**: nothing is created or deleted unless you pass `--param apply=yes`.

```bash
# Azure: sign in, then allow the subscription you mean to use
az login
export LABAGENT_ALLOWED_SUBSCRIPTIONS=<subscription id>
labagent launch dbx-workspace --name team1 --param cloud=azure                     # plan only
labagent launch dbx-workspace --name team1 --param cloud=azure --param apply=yes   # create it

# AWS: AWS credentials, plus the OAuth credentials of an account-level Databricks service principal
export LABAGENT_ALLOWED_ACCOUNTS=<aws account id>
export DATABRICKS_CLIENT_ID=... DATABRICKS_CLIENT_SECRET=...
labagent launch dbx-workspace --name team1 --param cloud=aws --param account_id=<databricks account id>
labagent launch dbx-workspace --name team1 --param cloud=aws --param account_id=<id> --param apply=yes

labagent destroy dbx-workspace --name team1 --param cloud=azure                    # preview, deletes nothing
labagent destroy dbx-workspace --name team1 --param cloud=azure --param apply=yes  # delete for real
```

Install the Terraform CLI (1.9 or later, or OpenTofu) first. The pack finds it on your path, or set `LABAGENT_TF_BIN`.

## What "premium" means on each cloud

| | Azure | AWS |
|---|---|---|
| How the tier is set | The module sets `sku = "premium"` as a fixed value. A variable could not make it standard | Your **Databricks account plan** decides. Terraform cannot buy or change a plan |
| What the pack checks | The workspace reports the `premium` SKU | The workspace reports `PREMIUM` or `ENTERPRISE`. If it reports another tier, the launch fails and tells you |
| Before you start | An Azure subscription where you can create resources (Contributor) | A Databricks account on a Premium or Enterprise plan (through AWS Marketplace or a direct contract), and a service principal with account admin rights |

These facts come from the Terraform provider documentation (Databricks provider 1.140, azurerm 5.11). On AWS the provider's `pricing_tier` is reported by Databricks, and the provider documentation does not say how it maps to your plan. That is why the pack checks the result and does not assume it.

## What gets created

| Option | Azure | AWS |
|---|---|---|
| `network=managed` (default) | Resource group, premium workspace, and the managed resource group Azure creates for it. Clusters have no public IP | IAM cross-account role and policy, a private encrypted root S3 bucket, and the Databricks workspace. Databricks creates the VPC |
| `network=custom` | Adds a virtual network, two delegated subnets and a network security group, and injects the workspace into it | Adds a VPC with a NAT gateway, S3, STS and Kinesis endpoints, and registers it |
| `hardened=yes` (Azure) | Adds the second layer of file system encryption and automatic cluster updates. Both are premium-only | Not available |

`network=custom` on AWS adds a NAT gateway and interface endpoints, which AWS bills by the hour. Clusters and SQL warehouses cost money only while they run, on both clouds.

It creates the workspace and its cloud plumbing only. It does not create users, clusters, catalogs or Unity Catalog. Manage the inside of the workspace with a second Terraform configuration, as the Databricks provider guides recommend.

## Safety

- **Plan first.** `apply=yes` applies the saved plan file. The pack never runs `-auto-approve`.
- **Allow lists.** It refuses to run unless the AWS account or Azure subscription is on `LABAGENT_ALLOWED_ACCOUNTS` or `LABAGENT_ALLOWED_SUBSCRIPTIONS`.
- **No secrets in files.** The Databricks client secret and cloud credentials are read from the environment by Terraform. They are never parameters, var files, state or output. Errors are cleaned of secret values, colour codes and email addresses.
- **No silent replacement.** If a plan would delete or replace anything while creating, the pack stops. A workspace is replaced when its network or region changes, and that loses its notebooks.
- **Destroy keeps your data by default.** On AWS the root bucket is not force-deleted, so `destroy` stops if the bucket holds files. Add `--param force_destroy=yes` to delete them too. A destroy without `apply=yes` is a preview and reports itself as failed so the pack keeps its records.
- **Pinned providers.** Each module ships a lock file with hashes for macOS, Linux and Windows, and `terraform init` runs with `-lockfile=readonly`.
- **Tags.** Every resource carries `Owner`, `ManagedBy=labagent` and `Instance`. `owner` must be a team name, not an email address.

## State

`backend=local` (default) keeps the Terraform state in `~/.labagent/packs/dbx-workspace/<name>.tfstate`. If you lose that file you lose track of the workspace, so use a remote backend for anything you keep: `--param backend=s3 --param state_bucket=<bucket>` or `--param backend=azurerm --param state_storage=<resource group>/<storage account>/<container>`. Runs on a CI runner need a remote backend, because the runner's disk disappears.

## Options

`cloud` (required), `region` (default `us-east-1` or `eastus2`), `network`, `apply`, `account_id` (AWS), `subscription_id` (Azure), `profile` (AWS), `hardened` (Azure), `owner`, `force_destroy` (AWS), `backend`, `state_bucket`, `state_storage`.

## What has been tested

- Both modules pass `terraform validate` against the real provider schemas and `terraform test` with mocked providers (fixed premium SKU, network modes, private bucket, input checks).
- The pack is tested with a fake Terraform: plan-only by default, apply of the saved plan, the tier check, the allow lists, the clean-up of errors, and destroy.
- A real `terraform init -lockfile=readonly` and `validate` ran from the pack's own working copy for both clouds.
- **AWS, `network=managed`:** applied once against a real AWS account and Databricks account, and the workspace was created. That run found three faults, now fixed: the wrong IAM policy type for a Databricks-created VPC, a missing account ID on the Databricks provider, and no wait after a policy change.
- **Not yet applied:** Azure, and AWS with `network=custom`. Run the plan first, read it, and expect to fix small things on the first real apply. Two details were not confirmed: the exact Azure subnet delegation actions (the module leaves them to Azure's defaults), and how an AWS account plan maps to `pricing_tier`.
- **If AWS denies VPC actions on a new role**, the role's policy may not have spread yet. Run the launch again.
