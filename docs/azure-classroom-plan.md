# Azure classroom pack: design plan

Status: draft for review. No code written yet.
Written: 2026-11-01. Prices and docs were read on that date.

## Purpose

Azure Lab Services (ALS) retires on June 28, 2027. After that date customers lose their lab accounts, lab plans and labs.
They keep their Azure Compute Gallery and the images in it. Source: [ALS retirement guide](https://learn.microsoft.com/en-us/azure/lab-services/retirement-guide).

The `azure-classroom` pack rebuilds the ALS workflow on standard Azure VMs: build a template, save it to a gallery,
publish one VM per student, schedules, idle shutdown, reset, browser access, teardown.
Every page of the docs says: "simulated with standard Azure VMs; not Azure Lab Services".

## What I verified, and what I did not

| Fact | Source | Confidence |
|---|---|---|
| ALS retires June 28, 2027. Gallery and images stay. | MS retirement guide | high |
| ALS exports only specialized images. | MS retirement guide, FAQ on images | high |
| `Get-Labs.ps1` writes a CSV with the columns you listed, plus SubscriptionId, SubscriptionName, LabPlanId, ResourceGroupName, Id. | Script source on GitHub | high |
| `Get-AzLabServicesUsage.ps1` does NOT write a CSV. It prints tables or text by `-DetailLevel` 1 to 4 and returns objects with `-PassThru`. | Script source on GitHub | high |
| Specialized images: no `osProfile`, same accounts and passwords as the source VM, same computer name. | [Compute Gallery docs](https://learn.microsoft.com/en-us/azure/virtual-machines/shared-image-galleries) | high |
| Image definition needs publisher, offer, sku, OS type, OS state. Version format is Major.Minor.Patch. | same | high |
| VM can be the source of an image version (needs API 2023-07-03 or newer). | same | high |
| `Microsoft.DevTestLab/schedules` API `2018-09-15`, task type `ComputeVmShutdownTask`, daily recurrence, time zone ID, optional notification. | [ARM reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.devtestlab/schedules) | high |
| The schedule resource must be named `shutdown-computevm-<vmname>` to show up as the VM's auto-shutdown. | my memory, the reference page does not say it | low. Test in a spike. |
| Dv3, Dv4, Dv5, Ev3 to Ev5 and Fsv2 families support nested virtualization. B-series and A-series do not. | [search result summary](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series), the Hyper-V page I fetched has no size list | moderate. Check each size page before the allow-list ships. |
| Windows 10/11 client images need an eligible license (Windows Enterprise E3/E5, Education A3/A5, or VDA). The license is not in the VM price. Windows Server is. | [MS Q&A](https://learn.microsoft.com/en-us/answers/questions/2141337/am-i-eligible-for-windows-10-11-license-with-multi), the MS doc URL I tried returned 404 | moderate |
| New VNets created after Sept 30, 2025 have no default outbound internet. VMs need NAT Gateway, a public IP or a load balancer rule. | [Azure update](https://azure.microsoft.com/updates/default-outbound-access-for-vms-in-azure-will-be-retired-transition-to-a-new-method-of-internet-access) | high |
| Guacamole `user-mapping.xml` auth: users and connections in one file, changes apply without restart. Passwords may be hashed (the docs I read name md5; SHA-256 support not confirmed). | [Guacamole manual](https://guacamole.apache.org/doc/gug/configuring-guacamole.html) | moderate |
| The "50 students x 40 hours" ALS pricing example. | not found in the pages I read | unknown. Needed for tutorial 6. Source it before writing. |
| ALS Lab Unit prices. | not read | unknown |

## Decisions I need from you

1. **SDK Python floor.** Current Azure SDK releases need Python 3.10 or newer. labagent supports 3.9, and CI tests 3.9 and 3.12.
   Plan: `labagent[azure]` uses minimum versions only, so pip on 3.9 picks the last 3.9-compatible releases
   (identity 1.26, resource 25, compute 38, network 30.2 as of today). All Azure calls sit behind one gateway class with lazy imports.
   Unit tests use a fake gateway, like `test_rag_ide_ec2.py`, so CI does not need the SDK. Real calls only run on your Azure subscription.
   Accept?
2. **Where the gallery lives.** You asked for one resource group per lab, and also for gallery images to survive `destroy`.
   Deleting the lab resource group would delete a gallery inside it. Plan: a second resource group,
   `rg-labagent-azure-classroom-<name>-images`, holds the gallery. It carries the same tags and is deleted only with `delete_images=true`.
   The bring-your-own-image path never touches the customer's existing gallery. Accept?
3. **Who stops idle VMs.** An in-guest `shutdown` leaves an Azure VM stopped but still allocated, and still billed for compute.
   Only a deallocate stops the compute bill. So a systemd timer or scheduled task cannot do the job alone.
   - Option A (your spec): each VM gets a system-assigned managed identity with a role scoped to itself, and its idle script calls the Azure API to deallocate.
     Needs `azure-mgmt-authorization` and a deployer with `User Access Administrator` on the resource group.
   - Option B (my recommendation): the gateway VM has one managed identity (VM Contributor on the lab resource group) and runs a small controller.
     It reads active sessions from the Guacamole API, tracks hours per student per day, and deallocates idle or over-cap VMs.
     One role assignment instead of N. Works because Guacamole already sees every session.
   - For `access=rdp-ssh-direct` (no gateway) only the DevTest auto-shutdown at a fixed time applies, plus Option A as a flag.
   Choose A, B or both.
4. **Student start.** ALS lets a student start their own VM. A student here has no Azure rights, so they cannot start a deallocated VM.
   v1: the schedule starts VMs at class time, and `labagent start azure-classroom --student <id>` starts one. Self-service start goes in the feature map as "not in v1".
   Accept?
5. **Passwords.** Your spec says encrypted with `secrets_box`, never plain. Two facts:
   - The CLI `launch` does not pass a public key today (`_packs_main` calls `run_pack` with `pubkey_pem=None`). With no key, the envelope carries no secrets and nobody can read the passwords.
     Plan: add `--pubkey <file>` (or `LABAGENT_PUBKEY`) to `launch`, and a `labagent secrets decrypt --key private.pem` helper for instructors. No key means no passwords are issued: the run stops at preflight with a clear message.
   - Guacamole needs the VM password in plaintext to log a student in to RDP or SSH. Plan: the student only knows a Guacamole login password.
     The VM password lives in `user-mapping.xml` (mode 0600, inside the gateway) and is not given to the student. The Guacamole password is stored as a hash.
     Anyone with Contributor on the gateway VM can read its custom data, so the doc says to use a dedicated subscription for classrooms.
   `show_passwords`-style printing is off. The 0600 plaintext credentials file that rag-ide writes is not reused. Accept?
6. **Outbound internet.** Student VMs have no public IP, and new VNets have no default outbound access. A Linux admin lab that runs `apt install` would fail.
   Plan: param `outbound` = `none` (default) | `nat`. `nat` adds a NAT Gateway (hourly plus data charge, not priced here, look it up before enabling). The template VM gets a temporary public IP for the build and loses it before capture.
   Accept?
7. **Scheduling.** Pick `labagent schedule run`, a stateless command that GitHub Actions calls on a cron. Starts and stops VMs by tag and time table. No Automation account, no runbook, no extra Azure resource, login by OIDC.
   Weakness: if the CI job fails, a class does not start. Mitigation: `status` shows it, and `start` is one command. Azure Automation would need an account, a managed identity and a runbook per lab. Accept?
8. **Specialized images (the ALS path).** ALS only exports specialized images, so most customer images are specialized.
   A VM cloned from a specialized image has no `osProfile`: it keeps the template's accounts, passwords and computer name.
   Plan for `image_state=specialized`: after create, run a Run Command that sets the per-student password and a new computer name, then restarts.
   Windows caveat: clones share a machine SID, which is unsafe for AD domain controllers and some domain-joined setups. The planner flags it. Accept?
9. **Windows client OS** (Windows 10/11) is out of scope for v1. `os` is `linux` or `windows-server`. The planner flags client images as a licensing risk.
10. **CLI names.** `labagent reset`, `status`, `start`, `stop`, `schedule run`, `sweep-azure`, `als plan` are new top-level commands.
    `cli.py` sends any unknown first word to the plain-English translator, so each name must be added to `KNOWN`. `reset` also exists as an AWS job command in `run.py`. That is a different path (`labagent run --job`), so there is no clash, but I will document it.

## Design

### Package layout

```
labagent/packs/azure_classroom/
  pack.py        Pack subclass: params, steps, outputs
  gateway.py     AzureGateway: every SDK call, lazy imports, allow-list guard
  safety.py      tags, names, allow-lists, size and count caps, expiry
  recipes/       linux-admin (cloud-init), windows-ad (Custom Script Extension)
  guac.py        gateway cloud-init, user-mapping.xml writer, controller script
  prices.py      price table (labelled estimate), cost functions
labagent/als/    planner: CSV in, config and risk list out
```

### Auth and safety

- `DefaultAzureCredential`. No secrets in config files.
- Refuse to run unless the subscription ID is in `LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS` (comma list). Same shape as the AWS guard: empty list means refuse.
- Lab resource group: `rg-labagent-azure-classroom-<name>`. Name rules: slug of `--name`, lower case, 3 to 40 characters, so the group name stays under Azure's 90.
- Tags on every resource: `labagent=1`, `pack`, `name`, `cohort`, `created_at`, `expires_at`. Student VMs add `student=<id>`.
- Destroy lists resources in the lab group, deletes only those whose tags match this lab, then deletes the group only if tag `created_by_labagent=1` is on it and it is empty. An untagged resource blocks the group delete and is reported, never deleted.
- Caps: `max_students` default 30 (hard ceiling 100). VM size allow-list in `safety.py`.
  v1 list: `Standard_B1s`, `Standard_B2s`, `Standard_B2ms`, `Standard_D2s_v5`, `Standard_D4s_v5` and, for `nested=true`, `Standard_D4s_v5`, `Standard_D8s_v5`, `Standard_E4s_v5`. Any other size is refused.
- `labagent sweep-azure`: `plan` lists labs whose `expires_at` has passed, writes a plan with a hash (same idea as `labagent/plan.py`); `apply --plan x.json --hash H` deletes exactly that plan. The plan must be under 24 hours old.

### Steps (each has a read-only `check`)

| # | Step | Creates | Check |
|---|---|---|---|
| 1 | `preflight` | nothing | credential works, subscription allowed, quota (see below), image exists on the gallery path, public key present |
| 2 | `network` | RG, VNet, subnets (gateway, students), NSG, optional NAT Gateway | NSG and VNet exist with tags |
| 3 | `template` | temp VM, gallery RG, gallery, image definition, image version 1.0.0 | image version exists and `provisioningState=Succeeded` |
| 4 | `publish` | N student VMs, NICs, OS disks | VM exists with the student tag |
| 5 | `auto_shutdown` | one `DevTestLab/schedules` per VM, controller or in-guest idle check | schedule exists |
| 6 | `schedule` | tags that `schedule run` reads | tag present |
| 7 | `gateway` | gateway VM, static public IP with a DNS label, Docker, Guacamole | HTTPS login page returns 200 |
| 8 | `verify` | nothing | each VM reachable from the gateway, one test command per VM |
| 9 | `output` | nothing | n/a |

Order note: the gateway is built before `publish` finishes only in the sense that its user list is written after the VMs exist. I will keep your step numbers and write `user-mapping.xml` at the end of step 7, which runs after step 4.

**Quota preflight.** `compute.usage.list(location)` gives cores per VM family and the regional total. `compute.resource_skus` maps the size to its family.
Needed cores = (students + 2) x vCPU of the size, where the two extra are the gateway and a temporary template VM. Fail with the shortfall, the family name and the portal path to request more.

**Template step.**
1. Create the template VM from a marketplace image (Ubuntu 24.04 LTS; Windows Server 2022 Datacenter).
2. Run the recipe: cloud-init `customData` on Linux, Custom Script Extension on Windows.
3. Generalize: Linux `waagent -deprovision+user` by Run Command, then deallocate, then `virtual_machines.generalize`.
   Windows: `sysprep /generalize /oobe /shutdown`, wait for stopped, deallocate, generalize.
4. Create the image version from the VM (`source.virtualMachineId`).
5. Delete the template VM, its disk, NIC and temporary public IP. Keep the image. Write `gallery_image_id` to state.

Image definition: `hyperVGeneration=V2`, security type `Standard` (not Trusted Launch, so the same image works on the allow-listed sizes), publisher `labagent`, offer = recipe name, sku = `os`.
Generalized definitions and specialized definitions are separate, because OS state is fixed per definition. The pack makes both only when asked.

**Publish.** `ThreadPoolExecutor`, concurrency 5. Names: VM `vm-<name>-<student>`, Windows computer name `s<NN>-<hash4>` (limit 15 characters). Each student gets a password from `generate_password()` kept only in `ctx.mem["passwords"]`, then sealed by `_seal_passwords`. I will add a `kind` argument there so these are `vm_password` and `guacamole_password`, not `ide_password`.

**Reset.** Delete the student's VM and OS disk, create a new one from the image, re-issue the VM password, update `user-mapping.xml`. Student work on the disk is lost. That is the ALS "reimage" behaviour and the doc says so.

**Status.** One table: student, VM power state, hours run today (from the controller's log, or from instance view timestamps without a gateway), last session seen. Source of "last activity" is the Guacamole API, so it is blank for `rdp-ssh-direct`.

### Gateway

One Linux VM (B2s), Docker Compose: `guacd`, `guacamole/guacamole`, Caddy in front.
TLS: self-signed (`tls internal`) by default, as in `ec2.py`. With a real domain, a param. With `tls=letsencrypt` it uses the public IP's Azure DNS label (`<label>.canadacentral.cloudapp.azure.com`), so no sslip.io is needed.
NSG: inbound 443 only. Students have no inbound rule at all except from the gateway subnet on 22 and 3389.
Auth: file-based `user-mapping.xml`. One user per student with one connection (RDP or SSH). One instructor user with all connections. The file is mode 0600. Updated on reset without restarting the container.
If SHA-256 hashing is not supported in the Guacamole version, I will use md5 and say so in the doc, or move to the database auth backend. Spike item.

### Cost, canadacentral, list price in CAD

Read from the Azure Retail Prices API on 2026-11-01. Retail price, no reserved or spot discount. Estimate only. Stored in `prices.py` with that date.

| Item | CAD per hour |
|---|---|
| B2s Linux | 0.0651 |
| B2s Windows | 0.0764 |
| D2s_v5 Linux | 0.1502 |
| D2s_v5 Windows | 0.2793 |
| Standard static public IPv4 | 0.0070 |
| Standard SSD E4 (32 GiB), per month | 3.7059 (about 0.0051 per hour) |
| Standard SSD E10 (128 GiB), per month | 14.8236 (about 0.0203 per hour) |
| Gallery image storage, NAT Gateway | not priced here |

**Linux lab, 10 students** (B2s each, B2s gateway, E4 disks, one public IP):

| State | Calculation | CAD per hour |
|---|---|---|
| Running | 11 x 0.0651 + 11 x 0.0051 + 0.0070 | about 0.78 |
| Stopped (deallocated) | 11 x 0.0051 + 0.0070 | about 0.06 (about 45 per month) |

**Windows Server lab, 10 students** (D2s_v5 Windows each, Linux B2s gateway, 128 GiB disks for students, E4 for the gateway):

| State | Calculation | CAD per hour |
|---|---|---|
| Running | 10 x 0.2793 + 0.0651 + 10 x 0.0203 + 0.0051 + 0.0070 | about 3.07 |
| Stopped (deallocated) | 10 x 0.0203 + 0.0051 + 0.0070 | about 0.22 (about 157 per month) |

Not included: gallery image storage, egress, NAT Gateway, the one-time template build (about 30 to 60 minutes of one VM).
A 10-student, 40-hour Linux term is about 31 CAD of running cost plus about 45 CAD per month of idle disk and IP. A disk for every student runs all month, which is the biggest difference from ALS pricing and goes in tutorial 6.
Also, a deallocated VM keeps its disk and public IP charges, and the gateway must be running during class.

### Planner: `labagent als plan`

Input: the CSV from `Get-Labs.ps1`. Optional second input: usage. Because `Get-AzLabServicesUsage.ps1` has no CSV output, the doc tells users to run it with `-PassThru -DetailLevel 2` and pipe it to `Export-Csv`. Columns used: `LabName`, `TotalVirtualMachines`, `AssignedVMs`, `TotalStudents`, `TotalStudentsWithUsage`, `VM-Utilization (%)`. I will check these names against a real run before the docs promise them.
Per lab output: an `azure-classroom` config JSON, a cost estimate per term, and risks.

Mapping:

| CSV column | Config |
|---|---|
| `OsType` Linux / Windows | `os` linux / windows-server (client OS flagged, see risks) |
| `SkuName` | `vm_size` by a lookup table from ALS SKU names to the nearest allow-listed size. A name not in the table adds a warning and falls back to a size picked from the vCPU and memory in the name, if it has them. |
| `SkuCapacity` or `AssignedVMs` | `students`. `AssignedVMs` if the usage shows it, else `SkuCapacity`. |
| `IsCustomImage` true, `ImageSource` | `template=gallery`, `gallery_image_id=ImageSource`, `image_state=specialized` |
| `IsCustomImage` false | `template` = nearest recipe, plus a risk "marketplace image: needs a recipe" |
| `Location` | `region` |

Risks:
- Windows client OS: licensing check.
- Nested virtualization size: needs a D, E or F family size, not B-series.
- `IsAdvancedNetworking=true`: needs a network plan (peering or the existing subnet), not supported in v1.
- Marketplace image: needs a recipe, not a gallery import.
- Specialized image on Windows: duplicate SID warning.

I have not seen a real `SkuName` list. The lookup table is my guess from ALS naming and will be marked "check against your labs". The sample CSV is synthetic and labelled so.

## Pull requests

| PR | Content | Tests |
|---|---|---|
| 1 | Extra `azure`, `AzureGateway`, allow-list, tags, name rules, size and count caps, quota preflight, `destroy` by tag, `sweep-azure`, `--pubkey` | allow-list refusal, tag-only destroy, quota failure, sweep plan/apply, no password in log or envelope |
| 2 | Template step, recipes, gallery, image version, bring-your-own image check | step idempotency, resume after failure at each sub-step, generalize order |
| 3 | `publish`, auto-shutdown, `reset`, `status`, `start`/`stop`, `schedule run` | concurrency limit, reset keeps other students, specialized password reset |
| 4 | Guacamole gateway, controller (Option B) | user-mapping content, hash not plain, verify step |
| 5 | `als plan`, sample CSV | mapping on the 4 sample labs, each risk flag |
| 6 | Docs: `azure-classroom.md`, six tutorial pages, README, `e2e-azure-classroom.md` | `test_repo_hygiene` and `test_encoding` pass; docs have no em dashes |

After each PR I will list the Azure resources it creates and their hourly cost, using the table above.

## Risks and open items

1. I have no Azure subscription access from here. Every Azure call is tested only against a fake. The manual checklist in `docs/e2e-azure-classroom.md` is the real test, and you must run it. I will not claim the pack works on Azure until it has run.
2. Items marked low or unknown above need a spike on a real subscription: the schedule resource name, SHA-256 in Guacamole, nested-size list, `azure-mgmt-compute` minimum version for VM-sourced image versions.
3. ALS pricing and the "50 students x 40 hours" example need a source before tutorial 6.
4. Guacamole, Caddy and Docker images should be pinned by digest in PR 4, as `ec2.py` pins by commit.
5. The planner's SKU table is a guess until you share a real `Get-Labs.ps1` file.

## What I need from you to start PR 1

Answers to decisions 1 to 8 (a "yes to all recommendations" is fine), and confirmation that the target is a dedicated test subscription whose ID I should use in the e2e doc.
