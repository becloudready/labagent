# labagent: open-source hands-on Cloud & AI labs for technical training, workshops and hackathons

Currently works for AWS, Azure, Neo Cloud (Lambda, Nebius, Shadeform)

[![PyPI](https://img.shields.io/pypi/v/labagent)](https://pypi.org/project/labagent/)
[![Python](https://img.shields.io/pypi/pyversions/labagent)](https://pypi.org/project/labagent/)
[![CI](https://github.com/becloudready/labagent/actions/workflows/ci.yml/badge.svg)](https://github.com/becloudready/labagent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

labagent builds a lab with one command and removes it with one more. Each lab is a **pack**: ordered, re-runnable steps. `launch` resumes after a failure. `destroy` removes only what the pack created.

Built for people who run labs for other people:

* Trainers and instructor-led training teams who set up the same hands-on lab for 5 to 50 learners every cohort
* Bootcamps, training academies and college courses that need GPU, Databricks, Azure or AWS virtual labs without buying a lab platform
* GPU and cloud providers who want developer adoption through workshops and hackathons running on their own capacity

## Install 
Mac
```bash
brew install pipx && pipx ensurepath
pipx install labagent
```
Windows
```powershell
# Install prerequisites & labagent
winget install Python.Python.3.14
Invoke-WebRequest -Uri https://bootstrap.pypa.io/get-pip.py -OutFile get-pip.py
python get-pip.py
```
Linux
```bash
pip install labagent
```
## Use
```bash
labagent packs                                   # list labs (add --json for a machine-readable catalog)
labagent launch <pack> --name demo --param k=v   # build or resume
labagent destroy <pack> --name demo              # remove what launch created
```

## Packs

| Pack | What it builds | Where |
|---|---|---|
| `azure-labs` | Copy Azure VMs, disks and network from one resource group to another with snapshots, or snapshot a dead VM or disk. Source is read-only. An Azure Lab Services alternative. [Guide](docs/azure-labs.md) | Azure (needs `az login`) |
| `gpu-notebooks` | Rent a GPU VM (Shadeform) and give each GPU slice its own JupyterLab, with a URL and login per lab. [Guide](docs/gpu-notebooks.md). See what to rent first: `labagent compute offers --cloud shadeform --gpu H100` ([compute](docs/compute.md)) | Shadeform (API key) |
| `db-agent` | [db-agent](https://github.com/db-agent/db-agent) text-to-SQL app on Databricks Apps, pinned commit. [Which model, warehouse and schema it picks](docs/packs.md) | Databricks Free Edition |
| `dbx-lakehouse` | Medallion lakehouse (bronze, silver, gold) from real open data, with masks, a row filter, notebooks and exercises. [Guide](docs/dbx-lakehouse.md) | Databricks Free Edition, AWS, Azure |
| `dbx-workspace` | Premium Databricks workspace via Terraform. Plan only unless `apply=yes`. [Guide](docs/dbx-workspace.md) | AWS or Azure (costs money) |
| `rag-ide` | Private browser VS Code per learner with the RAG lab | Modal, EC2 or Docker |

## Workshops, hackathons and instructor-led training

Put the whole event in one YAML file. Launch every learner's sandbox before the session, destroy them after, and pay only for the hours used:

```bash
labagent launch -f hackathon.yaml    # one lab per learner
labagent destroy -f hackathon.yaml   # removes everything the
```
## Azure Lab Services is retiring (June 28, 2027)

```bash
export LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS=$(az account show --query id -o tsv)
labagent launch azure-labs --name move1 --param source_rg=legacy-rg --param target_rg=new-rg
labagent launch azure-labs --name old1 --param source_rg=legacy-rg --param target_rg=archive-rg --param snapshot_only=true
labagent destroy azure-labs --name move1 --param target_rg=new-rg
```

## Teaching with labagent?

labagent is free and open source. If you run cohorts and would rather not run the environments yourself, BeCloudReady runs managed hands-on labs for a fixed price per cohort, and builds custom packs for your course.

* Trainers and training companies: we're taking a few design partners for early 2027 cohorts, at cloud cost. Details on /labs.
* GPU and cloud providers: want a pack, hackathon or workshop driving developer adoption on your capacity? Open an issue or reach out through becloudready.com/labs.
* Upcoming: free GPU fine-tuning hackathon with TorontoAI, Dec 16, 2026, 5 PM ET. Every participant gets a GPU lab launched by labagent.

Apache-2.0. [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), [docs/packs.md](docs/packs.md). Related: [db-agent](https://github.com/db-agent/db-agent), [labagent-datasets](https://github.com/becloudready/labagent-datasets). Maintained by [BeCloudReady](https://becloudready.com/).
