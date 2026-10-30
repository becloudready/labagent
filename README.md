# labagent: hands-on labs CLI for RAG, text-to-SQL, Databricks and AWS sandboxes

> Launch a hands-on lab with one command. Remove it with one more. Labs for **RAG**, **text-to-SQL** on **Databricks Lakebase** (pgvector), a **browser VS Code per learner**, and **AWS sandboxes** with Terraform.

[![PyPI](https://img.shields.io/pypi/v/labagent)](https://pypi.org/project/labagent/)
[![Python](https://img.shields.io/pypi/pyversions/labagent)](https://pypi.org/project/labagent/)
[![CI](https://github.com/becloudready/labagent/actions/workflows/ci.yml/badge.svg)](https://github.com/becloudready/labagent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Databricks Free Edition](https://img.shields.io/badge/Databricks-Free%20Edition-FF3621)](docs/packs.md)

**Related:** [DB Agent](https://github.com/db-agent/db-agent) | [labagent-datasets](https://github.com/becloudready/labagent-datasets) | [BeCloudReady workshops](https://github.com/becloudready/workshops)

## What it does

A lab is a **pack**: ordered steps that are safe to re-run. `launch` builds it and resumes after a failure. `destroy` removes only what the pack created.

- **Databricks labs.** A step-by-step RAG tutorial over real SEC 10-K filings (notebooks you run), and a text-to-SQL agent, in your own Free Edition workspace.
- **Web IDE per learner.** Private browser VS Code with the RAG lab preinstalled, on Modal, EC2 or local Docker.
- **AWS sandboxes.** Cohort IAM with Terraform, plus plan-then-apply cleanup of shared accounts.
- **Plain English.** `labagent "deploy rag-tutorial"` shows the command it understood and asks first (local Ollama, any OpenAI-compatible endpoint).

## Install

The packs and extras described here are on `main`. The release on PyPI (0.2.1) is older: it has no `aml`, `dbx-lakehouse`, `dbx-workspace`, `rag-ide` or `rag-sec-tutorial` pack and no `rag-local` or `aml` extra. Until the next release, install from GitHub. These lines work in bash, PowerShell and cmd:

```
python -m pip install --upgrade pip
pip install "labagent[rag-local,aml] @ git+https://github.com/becloudready/labagent"
labagent packs                         # you should see aml, dbx-lakehouse, rag-sec-tutorial ...
```

Use `labagent[rag]` for the Databricks RAG tutorial, or a clone with `pip install -e ".[rag-local,aml]"` if you want to edit the code.

## Quick start

```bash
pip install "labagent[rag] @ git+https://github.com/becloudready/labagent"      # Python 3.9+; sign in with: databricks auth login
labagent packs                         # list the labs
labagent launch rag-sec-tutorial --name demo     # uploads the notebooks to your workspace; you run them
labagent destroy rag-sec-tutorial --name demo    # removes the uploaded folder
```

## Labs

| Pack | What you get | Runs on |
|---|---|---|
| `rag-sec-tutorial` | Nine notebooks that build RAG on a pinned SEC 10-K dataset one step at a time: embeddings, pgvector, hybrid search, filters, citations, an evaluation gate, and the deployed app. Each ends with a check cell | Databricks Free Edition |
| `db-agent` | [db-agent](https://github.com/db-agent/db-agent) text-to-SQL over a Databricks sample dataset, pinned commit | Databricks Free Edition |
| `rag-ide` | A private browser VS Code per learner with the RAG lab and notebooks | Modal, EC2 or local Docker |
| `dbx-workspace` | A premium Databricks workspace in your own AWS account or Azure subscription, created with Terraform. Plan first: nothing is created without `apply=yes`. AWS can add your users to the workspace. [Guide](docs/dbx-workspace.md) | AWS or Azure (costs apply) |
| `dbx-lakehouse` | A ready-to-teach medallion lakehouse: bronze, silver and gold tables in Unity Catalog from real open data (online retail, synthetic healthcare), with masks, a row filter, SQL notebooks and exercises. [Guide](docs/dbx-lakehouse.md) | Databricks Free Edition, AWS or Azure |
| `aml` | A synthetic anti-money laundering lab with an answer key: a RAG lab for developers (`mode=rag`) and a Microsoft 365 Copilot lab for staff (`mode=copilot`), for Canada or the US. [Guide](docs/aml.md) | Your machine, no cloud account |

Free Edition limits apply (one Lakebase project, three apps). Details: [docs/packs.md](docs/packs.md).

## AML lab (synthetic data, RAG and Copilot)

```bash
pip install "labagent[rag-local,aml] @ git+https://github.com/becloudready/labagent"
labagent launch aml --name demo --param mode=rag --param jurisdiction=ca        # developers: indexed corpus, helper, quality checks
labagent launch aml --name spring --param mode=copilot --param jurisdiction=ca   # staff: Excel and Word files, 13 exercises, instructor folder
```

It generates a made-up credit union with 8 laundering patterns, an answer key for every alert, policies, case files and regulation text. The labels come from how the data is built, not from an investigator. See [docs/aml.md](docs/aml.md).

## Premium Databricks workspace (AWS or Azure)

```bash
# AWS: AWS credentials, plus the OAuth client id and secret of an account-level Databricks service principal
export LABAGENT_ALLOWED_ACCOUNTS=<aws account id> DATABRICKS_CLIENT_ID=... DATABRICKS_CLIENT_SECRET=...
labagent launch dbx-workspace --name team1 --param cloud=aws --param account_id=<databricks account id>                 # plan only
labagent launch dbx-workspace --name team1 --param cloud=aws --param account_id=<id> --param apply=yes \
  --param admin_emails=you@example.com                                                                                  # create, and give you access
labagent destroy dbx-workspace --name team1 --param cloud=aws --param account_id=<id> --param apply=yes --param force_destroy=yes
```

Azure works the same with `--param cloud=azure` and `LABAGENT_ALLOWED_SUBSCRIPTIONS` after `az login`. It needs the Terraform CLI (1.9 or later) or OpenTofu. Nothing is created without `apply=yes`, and the pack refuses accounts that are not on the allow list.

Status: the AWS workspace with a Databricks-created network has been created end to end in a real account. Azure, `network=custom` and `admin_emails` are tested only with mocked providers so far. See [docs/dbx-workspace.md](docs/dbx-workspace.md).

## Teaching lakehouse (bronze, silver, gold)

```bash
databricks auth login
labagent launch dbx-lakehouse --name lab1                      # retail + healthcare, built and checked
labagent destroy dbx-lakehouse --name lab1                     # drops what the pack created
```

Real open data (UCI Online Retail II, Synthea synthetic patients) carried through all three layers in Unity Catalog, with column masks, a row filter, a sandbox schema, and SQL notebooks with exercises. Needs only a SQL warehouse, so it runs on Free Edition, AWS and Azure. Status: built and destroyed on Databricks Free Edition. AWS and Azure not run yet. See [docs/dbx-lakehouse.md](docs/dbx-lakehouse.md).

## Web IDE per learner (unreleased: install from GitHub)

```bash
pip install 'labagent[modal] @ git+https://github.com/becloudready/labagent'
labagent launch rag-ide --name cohort --param learners=2 --param platform=modal
```

| `platform` | How | Needs |
|---|---|---|
| `modal` (default) | One serverless sandbox per learner | `modal token new` |
| `ec2` | One shared instance per cohort, a container per learner, HTTPS | An AWS profile (`--param profile=...`) in `LABAGENT_ALLOWED_ACCOUNTS` |
| `local` | Containers on this machine, loopback only | Docker |

Each learner gets a URL and a password (credentials file, mode 0600). Sessions stop when idle and at a time cap (`hours`, default 4). Tested with small cohorts, not at 30 learners.

There is also a laptop-only, zero-cost RAG lab where learners build a chunker, a vector store, hybrid search and an evaluation themselves, one notebook at a time:

```bash
pip install "labagent[rag-lab] @ git+https://github.com/becloudready/labagent"   # includes JupyterLab
labagent rag init --dir ~/rag-lab
cd ~/rag-lab && jupyter lab                                                      # then open notebooks/01_chunking.ipynb
```

On Windows PowerShell use `$HOME\rag-lab` and `cd $HOME\rag-lab`. If you installed `rag-local` instead, add `pip install jupyterlab`, or open the notebooks in VS Code and pick the Python environment you installed into as the kernel. See [docs/rag-local.md](docs/rag-local.md).

## AWS sandboxes

```bash
labagent --config lab.json run --job job.json --output result.json
```

| Command | What it does |
|---|---|
| `provision`, `offboard` | Create or destroy a cohort's IAM layer with Terraform |
| `inventory`, `sweep` | List and delete learner resources: plan first, then apply that reviewed plan |
| `reset` | Rotate a password (returned encrypted to your public key) |
| `audit`, `cleanup` | Clean a long-lived account from a ruleset |

See [job-contract](docs/job-contract.md), [state and secrets](docs/state-and-secrets.md), [account cleanup](docs/account-cleanup.md).

## Safety

- Plan first, then apply that exact plan. Only a `delete` rule can delete; unmatched resources are reported, never removed.
- AWS commands refuse to run in an account that is not on your allow list.
- Labs use your own credentials. The db-agent token is stored as an app secret and revoked on `destroy`.
- Tests keep secrets and personal data out of the repo and out of logs.

Early release: try labs in a throwaway workspace or account first. Apache-2.0. See [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

Maintained by [BeCloudReady](https://becloudready.com/).
