# Azure Labs pack: design plan (v2)

Status: draft for review. No code written yet.
Updated: 2026-11-01. Replaces the first draft (pack name `azure-classroom`, Python SDK, cost section).

## Purpose

Azure Lab Services (ALS) retires on June 28, 2027. Customers lose their lab accounts, lab plans and labs, and keep their Azure Compute Gallery and its images.
Source: [ALS retirement guide](https://learn.microsoft.com/en-us/azure/lab-services/retirement-guide).

The `azure-labs` pack rebuilds the ALS workflow on standard Azure VMs: template, gallery image, one VM per student, schedules, idle shutdown, reset, browser access, teardown.
It is an alternative to Azure Lab Services. It is not Microsoft's service and is not affiliated with Microsoft. Every doc page says so in the first lines.

Goals now: working functionality, content for a YouTube series, and a test path a junior engineer can run. Cost features are out of scope.

## Scope changes from the first draft

| Change | Why |
|---|---|
| Pack id `azure-labs` (was `azure-classroom`) | Search term. Docs: `docs/azure-labs.md`, `docs/azure-labs/`. |
| Azure calls go through the `az` CLI, not the Python SDK | You are signed in with `az`. Removes the Python 3.9 SDK problem. Viewers can copy the commands. No `azure` extra. |
| No cost command, no price table, no cost column | Out of scope for now. |
| Simulation uses one subscription, two resource groups | No ALS account is available. See below. |
| Sizes follow the quota of the test subscription | 10 vCPUs per region, Dsv5 family quota 0. |

## How the simulation works

You have no ALS lab, so the "old lab" is a stand-in. Everything after the gallery image is real.

1. **Legacy lab (stand-in).** The template step builds a template VM, runs the recipe, and saves it as a gallery image. For the ALS case it is captured as a specialized image, because ALS exports only specialized images.
2. **Inventory.** A sample `Get-Labs.ps1` CSV, labelled "sample output, not from a real customer", describes that lab. `labagent als plan` reads it.
3. **New lab.** `labagent launch azure-labs --param template=gallery --param gallery_image_id=<id> --param image_state=specialized` rebuilds the class in a new resource group.
4. **Moving between accounts.** Run for real: replicate the image to a second region (eastus2). Documented but not run: share the gallery to another subscription by role assignment, since only one subscription exists.

On camera: say what is simulated (the ALS lab) and what is real (gallery, VMs, access, reset, teardown).

## What I verified, and what I did not

| Fact | Source | Confidence |
|---|---|---|
| ALS retires June 28, 2027. Gallery and images stay. | MS retirement guide | high |
| ALS exports only specialized images. | MS retirement guide | high |
| `Get-Labs.ps1` writes a CSV with the columns you listed plus SubscriptionId, SubscriptionName, LabPlanId, ResourceGroupName, Id. | script source | high |
| `Get-AzLabServicesUsage.ps1` writes no CSV. It prints tables by `-DetailLevel` 1 to 4 and returns objects with `-PassThru`. | script source | high |
| Specialized images: no `osProfile`, same accounts, passwords and computer name as the source VM. | [Compute Gallery docs](https://learn.microsoft.com/en-us/azure/virtual-machines/shared-image-galleries) | high |
| Image definition needs publisher, offer, sku, OS type and OS state. Versions are Major.Minor.Patch. A VM can be the source of a version. | same | high |
| `Microsoft.DevTestLab/schedules`, API `2018-09-15`, task type `ComputeVmShutdownTask`, daily time and time zone ID. | [ARM reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.devtestlab/schedules) | high |
| The schedule must be named `shutdown-computevm-<vmname>` to show as the VM's auto-shutdown. | my memory | low. Spike. |
| Dv3, Dv4, Dv5, Ev3 to Ev5 and Fsv2 support nested virtualization. B-series does not. | search summary | moderate. Spike. |
| Windows 10/11 client images need an eligible license. Windows Server is in the VM price. | [MS Q&A](https://learn.microsoft.com/en-us/answers/questions/2141337/am-i-eligible-for-windows-10-11-license-with-multi) | moderate |
| New VNets created after Sept 30, 2025 have no default outbound internet. | [Azure update](https://azure.microsoft.com/updates/default-outbound-access-for-vms-in-azure-will-be-retired-transition-to-a-new-method-of-internet-access) | high |
| Guacamole `user-mapping.xml` auth works from one file and applies changes without restart. md5 hashing is documented, SHA-256 not confirmed. | [Guacamole manual](https://guacamole.apache.org/doc/gug/configuring-guacamole.html) | moderate |
| Test subscription: pay-as-you-go, 10 vCPUs per region, Dsv5 quota 0, Dv3 and B-series 10, you are Owner, providers registered. | `az` read-only checks, 2026-11-01 | high |

## Decisions (defaults I will use unless you say otherwise)

1. **`az` CLI, not the SDK.** Done. Each student VM deploys through an ARM template with `az deployment group create`. The password goes in a secure parameter read from a 0600 temp file that is deleted after. It never appears in process arguments or logs.
2. **Gallery in a second resource group** `rg-labagent-azure-labs-<name>-images`, deleted only with `delete_images=true`. The bring-your-own-image path never touches an existing gallery.
3. **Idle shutdown and hours cap run on the gateway** (it has one managed identity with VM Contributor on the lab group, reads Guacamole sessions, deallocates idle or over-cap VMs). Without a gateway (`access=rdp-ssh-direct`), only the fixed-time auto-shutdown applies. An in-guest shutdown does not stop the compute bill, only a deallocate does.
4. **Student start** is not in v1. The schedule starts VMs. `labagent start azure-labs --name X --student S` starts one.
5. **Passwords** are sealed with `secrets_box`. `launch` gets `--pubkey <file>` (or `LABAGENT_PUBKEY`). No key means the run stops at preflight. A `labagent secrets decrypt --key private.pem` helper reads them. Guacamole stores the VM password in a 0600 file inside the gateway. The student never sees it.
6. **Outbound internet.** Param `outbound`: `none` (default) or `nat`. The template VM gets a temporary public IP for the build and loses it before capture.
7. **Schedules** run through `labagent schedule run`, a stateless command meant for a GitHub Actions cron.
8. **Specialized path.** After create, a Run Command sets a per-student password and a new computer name. Windows clones share a machine SID. The planner flags it for AD labs.
9. **Windows client OS is out of scope.** `os` is `linux` or `windows-server`.
10. **New top-level commands** `reset`, `status`, `start`, `stop`, `schedule run`, `sweep-azure`, `als plan`, `secrets decrypt` go into `KNOWN` in `cli.py`, or the plain-English translator would catch them.

## Design

### Layout

```
labagent/packs/azure_labs/
  pack.py        Pack subclass: params, steps, outputs
  azcli.py       AzCli: every az call, JSON in and out, UTF-8, secure temp files
  safety.py      tags, names, allow-lists, size and count caps, expiry
  recipes/       linux-admin (cloud-init), windows-ad (Custom Script Extension)
  arm/           vm.json, schedule.json (ARM templates)
  guac.py        gateway cloud-init, user-mapping.xml writer, controller script
labagent/als/    planner: CSV in, config and risk list out
```

### Auth and safety

- Uses the signed-in `az` session (`az login`, or OIDC in CI). No secrets in config.
- Refuse unless the subscription ID is in `LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS` (comma list). Empty means refuse.
- Lab group `rg-labagent-azure-labs-<name>`. Name: lower case, 3 to 40 characters.
- Tags on every resource: `labagent=1`, `pack`, `name`, `cohort`, `created_at`, `expires_at`. Student VMs add `student`.
- `destroy` lists the lab group, deletes only resources with this lab's tags, and deletes the group only if it has `created_by_labagent=1` and is then empty. An untagged resource blocks the group delete and is reported.
- Caps: `max_students` default 30, ceiling 100. Size allow-list: `Standard_B1s`, `B2s`, `B2ms`, `D2s_v3`, `D4s_v3`, `D2s_v5`, `D4s_v5`. For `nested=true`: `D2s_v3`, `D4s_v3`, `D4s_v5`, `D8s_v5`, `E4s_v5`. Anything else is refused.
- Defaults: linux `Standard_B2s`, windows-server `Standard_D2s_v3`. The preflight error names the family and the portal path when quota is short.
- `labagent sweep-azure plan` lists labs past `expires_at` and writes a plan with a hash (same idea as `labagent/plan.py`). `apply --plan x.json --hash H` deletes exactly that plan, if it is under 24 hours old.

### Steps (each has a read-only `check`)

| # | Step | Creates | Check |
|---|---|---|---|
| 1 | `preflight` | nothing | `az account show`, subscription allowed, quota, image exists, public key present |
| 2 | `network` | RG, VNet, subnets, NSG, optional NAT Gateway | NSG and VNet exist with tags |
| 3 | `template` | temp VM, gallery RG, gallery, image definition, image version 1.0.0 | version exists, state Succeeded |
| 4 | `publish` | student VMs | VM with the student tag exists |
| 5 | `auto_shutdown` | one `DevTestLab/schedules` per VM | schedule exists |
| 6 | `schedule` | tags that `schedule run` reads | tags present |
| 7 | `gateway` | gateway VM, static IP with DNS label, Guacamole | login page returns 200 |
| 8 | `verify` | nothing | each VM reachable from the gateway, one test command per VM |
| 9 | `output` | nothing | n/a |

The gateway's user list is written at the end of step 7, after step 4 has made the VMs.

**Quota preflight.** `az vm list-usage` gives cores by VM family and region. Count the peak at any moment: the template phase (template VM) and the publish phase (students plus gateway) never overlap, so use the larger. A reset needs one extra slot only if create runs before delete, so reset deletes first.

**Template step.**
1. Create the template VM from a marketplace image (Ubuntu 24.04 LTS, Windows Server 2022 Datacenter).
2. Run the recipe: cloud-init on Linux, Custom Script Extension on Windows.
3. Generalized path: Linux `waagent -deprovision+user`, Windows `sysprep /generalize /oobe /shutdown`, then deallocate and `az vm generalize`.
   Specialized path: deallocate only.
4. `az sig image-version create` from the VM.
5. Delete the template VM, disk, NIC and temporary IP. Keep the image. Save `gallery_image_id` in state.

Image definition: Hyper-V generation V2, security type Standard, publisher `labagent`, offer = recipe name, sku = os. Generalized and specialized images need separate definitions.

**Publish.** Concurrency 5. VM name `vm-<name>-<student>`. Windows computer name `s<NN>-<hash4>` (15-character limit). Passwords exist only in memory until sealed.

**Reset.** Delete the student's VM and disk, create a new one from the image, issue a new password, update `user-mapping.xml`. Student work on that disk is lost, as in ALS reimage.

**Status.** Table: student, power state, hours run today, last session seen. "Last session" comes from the Guacamole API and is blank without a gateway.

### Gateway

One Linux VM (B2s). Docker Compose: `guacd`, `guacamole/guacamole`, Caddy. TLS self-signed by default, a param for a real domain, and `tls=letsencrypt` using the public IP's Azure DNS label. NSG inbound 443 only. Students accept 22 and 3389 only from the gateway subnet. Auth by `user-mapping.xml`: one user per student, one instructor with every connection. If SHA-256 is not supported, use md5 and say so in the docs.

### Planner: `labagent als plan`

Input: the `Get-Labs.ps1` CSV. Optional usage CSV, made by running `Get-AzLabServicesUsage.ps1 -PassThru -DetailLevel 2 | Export-Csv`. Column names used from it: `LabName`, `TotalVirtualMachines`, `AssignedVMs`, `TotalStudents`, `TotalStudentsWithUsage`, `VM-Utilization (%)`. I will check these against a real run before the docs promise them.

| CSV column | Config |
|---|---|
| `OsType` | `os` |
| `SkuName` | `vm_size`, by a lookup table. A name not in the table adds a warning. |
| `SkuCapacity` or `AssignedVMs` | `students` |
| `IsCustomImage` true, `ImageSource` | `template=gallery`, `gallery_image_id`, `image_state=specialized` |
| `IsCustomImage` false | nearest recipe, risk "marketplace image" |
| `Location` | `region` |

Risks flagged: Windows client OS, nested virtualization size, `IsAdvancedNetworking=true`, marketplace image, specialized Windows (SID).

Sample file `examples/als/sample-get-labs.csv`: four labs (Linux admin, Windows Server AD, cybersecurity with nested virtualization, advanced networking), labelled "sample output, not from a real customer".

## Testing and junior validation

Unit tests use a fake `AzCli` (same pattern as `FakeEc2` in `test_rag_ide_ec2.py`): idempotent steps, resume after each failing step, tag-only destroy, subscription refusal, quota failure, no password in logs or envelope, sweep plan and apply, planner mapping and risk flags. They run without Azure.

`docs/e2e-azure-labs.md` is written for a junior engineer. Each test has: the exact command, the expected output, an `az` command that proves it, and pass or fail. Tests:

| # | Test | Proves |
|---|---|---|
| 1 | Refusal: run with the subscription not in the allow-list | guard works, nothing created |
| 2 | `launch --dry-run` | steps listed, nothing created |
| 3 | Linux template to gallery image (specialized and generalized) | image version exists |
| 4 | Launch 3 students from that image | 3 VMs, tagged, no public IP |
| 5 | Open the gateway URL, log in as a student, reach the VM | browser access |
| 6 | Auto-shutdown schedule shows on a VM | schedule resource |
| 7 | Reset one student | that VM recreated, others untouched |
| 8 | `als plan` on the sample CSV | 4 configs and the expected risk flags |
| 9 | Windows Server AD with 2 students | AD tools present |
| 10 | Destroy | lab group gone, image kept |
| 11 | Destroy with `delete_images=true` | image group gone |
| 12 | Put an untagged resource in the lab group, destroy | it is reported, not deleted |

Each test ends with a cleanup check: `az resource list --tag labagent=1` returns nothing for that lab.

## Pull requests

| PR | Content |
|---|---|
| 1 | `AzCli`, allow-list, tags, names, caps, quota preflight, `destroy` by tag, `sweep-azure`, `--pubkey`, `secrets decrypt` |
| 2 | Template step, recipes, gallery, image version, bring-your-own image check |
| 3 | `publish`, auto-shutdown, `reset`, `status`, `start`, `stop`, `schedule run` |
| 4 | Guacamole gateway and controller |
| 5 | `als plan` and the sample CSV |
| 6 | Docs: `azure-labs.md`, six tutorial pages, README, `e2e-azure-labs.md` |

After each PR: tests pass, and I list the Azure resources that PR creates.

## Risks

1. I cannot reach your Azure subscription from the tests. Every Azure call is tested against a fake. The pack counts as working only after the junior engineer completes the e2e table.
2. Items marked low or moderate above need a spike on a real subscription: schedule resource name, Guacamole SHA-256, nested-size list.
3. The planner's SKU table is a guess until you share a real `Get-Labs.ps1` file.
4. Guacamole, Caddy and Docker images get pinned by digest in PR 4.
