# labagent: CLI to launch hands-on labs for RAG, text-to-SQL, Databricks, AWS and Azure

[![PyPI](https://img.shields.io/pypi/v/labagent)](https://pypi.org/project/labagent/)
[![Python](https://img.shields.io/pypi/pyversions/labagent)](https://pypi.org/project/labagent/)
[![CI](https://github.com/becloudready/labagent/actions/workflows/ci.yml/badge.svg)](https://github.com/becloudready/labagent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

labagent builds a lab with one command and removes it with one more. Each lab is a **pack**: ordered, re-runnable steps. `launch` resumes after a failure. `destroy` removes only what the pack created.

```bash
pip install labagent
labagent packs                                   # list labs (add --json for a machine-readable catalog)
labagent launch <pack> --name demo --param k=v   # build or resume
labagent destroy <pack> --name demo              # remove what launch created
```

Extras: `labagent[rag]` (Databricks RAG), `[rag-local]` (laptop RAG), `[rag-lab]` (laptop RAG with JupyterLab), `[aml]`, `[modal]`. Python 3.9+. Add `--dry-run` to any launch to see the steps.

## Packs

| Pack | What it builds | Where |
|---|---|---|
| `azure-labs` | Copy Azure VMs, disks and network from one resource group to another with snapshots, or snapshot a dead VM or disk. Source is read-only. An Azure Lab Services alternative. [Guide](docs/azure-labs.md) | Azure (needs `az login`) |
| `rag-sec-tutorial` | Nine notebooks: RAG on SEC 10-K filings with embeddings, pgvector, hybrid search, citations, an evaluation gate, a deployed app | Databricks Free Edition |
| `db-agent` | [db-agent](https://github.com/db-agent/db-agent) text-to-SQL app on Databricks Apps, pinned commit. [Which model, warehouse and schema it picks](docs/packs.md) | Databricks Free Edition |
| `dbx-lakehouse` | Medallion lakehouse (bronze, silver, gold) from real open data, with masks, a row filter, notebooks and exercises. [Guide](docs/dbx-lakehouse.md) | Databricks Free Edition, AWS, Azure |
| `dbx-workspace` | Premium Databricks workspace via Terraform. Plan only unless `apply=yes`. [Guide](docs/dbx-workspace.md) | AWS or Azure (costs money) |
| `aml` | Synthetic anti-money-laundering lab with an answer key: RAG for developers, Microsoft 365 Copilot for staff. [Guide](docs/aml.md) | Your machine |
| `rag-ide` | Private browser VS Code per learner with the RAG lab | Modal, EC2 or Docker |

Also: `labagent rag init --dir ~/rag-lab` makes a laptop RAG lab of nine notebooks (needs `[rag-lab]`), and `labagent rag destroy --dir ~/rag-lab` removes it. See [docs/rag-local.md](docs/rag-local.md). AWS cohort sandboxes (Terraform IAM, plan-then-apply cleanup) run through `labagent run --job job.json`: see [docs/job-contract.md](docs/job-contract.md).

## Azure: move VMs between resource groups

```bash
export LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS=$(az account show --query id -o tsv)
labagent launch azure-labs --name move1 --param source_rg=legacy-rg --param target_rg=new-rg
labagent launch azure-labs --name old1 --param source_rg=legacy-rg --param target_rg=archive-rg --param snapshot_only=true
labagent destroy azure-labs --name move1 --param target_rg=new-rg
```

Parameters: `source_rg`, `target_rg`, `vms` (comma list, default all), `snapshot_only`, `subscription`. Same subscription and region. It does not copy availability sets, scale sets, unmanaged VHDs, customer-key encryption, extensions or public IP addresses (a new address is created). Tested on a real subscription with Ubuntu VMs. Not Azure Lab Services and not from Microsoft.

## For AI coding agents and scripts

- Output is one JSON envelope on stdout (`status`: `ok`, `partial`, `failed`; `items` per step). Exit code 0, 1, 2. Logs go to stderr and never contain secrets.
- `labagent packs --json` lists every pack with parameters, defaults, platforms and cost tier.
- Every `launch` is idempotent. Run it again to resume. `destroy` deletes only resources carrying the pack's tags or state.
- Safety gates: `LABAGENT_ALLOWED_ACCOUNTS` (AWS), `LABAGENT_ALLOWED_SUBSCRIPTIONS` (Databricks on Azure), `LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS` (azure-labs). An empty list refuses to run.
- Plain English front end: `labagent "deploy rag-tutorial"` prints the command it understood and asks first (local Ollama or any OpenAI-compatible endpoint).

## Status

Run end to end on real services: Databricks Free Edition labs, the AWS premium workspace, and azure-labs copy and delete on Azure. Tested only against mocks so far: Azure and custom-network `dbx-workspace`, and `dbx-lakehouse` on AWS and Azure. Try labs in a throwaway workspace or account first.

Apache-2.0. [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), [docs/packs.md](docs/packs.md). Related: [db-agent](https://github.com/db-agent/db-agent), [labagent-datasets](https://github.com/becloudready/labagent-datasets). Maintained by [BeCloudReady](https://becloudready.com/).
