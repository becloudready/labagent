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

- **Databricks labs.** RAG over real SEC 10-K filings, and a text-to-SQL agent, in your own Free Edition workspace.
- **Web IDE per learner.** Private browser VS Code with the RAG lab preinstalled, on Modal, EC2 or local Docker.
- **AWS sandboxes.** Cohort IAM with Terraform, plus plan-then-apply cleanup of shared accounts.
- **Plain English.** `labagent "deploy rag-sec"` shows the command it understood and asks first (local Ollama, any OpenAI-compatible endpoint).

## Quick start

```bash
pip install 'labagent[rag]'            # Python 3.9+; sign in with: databricks auth login
labagent packs                         # list the labs
labagent launch rag-sec-filings --name demo
labagent destroy rag-sec-filings --name demo
```

## Labs

| Pack | What you get | Runs on |
|---|---|---|
| `rag-sec-filings` | RAG over a pinned SEC 10-K dataset: hybrid search, filters, citations, a quality gate before deploy | Databricks Free Edition |
| `rag-sec-filings-live` | Same, fetching fresh filings from EDGAR | Databricks Free Edition |
| `rag-lakebase` | Small demo corpus to smoke-test your setup | Databricks Free Edition |
| `db-agent` | [db-agent](https://github.com/db-agent/db-agent) text-to-SQL over a Databricks sample dataset, pinned commit | Databricks Free Edition |
| `rag-ide` | A private browser VS Code per learner with the RAG lab and notebooks | Modal, EC2 or local Docker |

Free Edition limits apply (one Lakebase project, three apps). Details: [docs/packs.md](docs/packs.md).

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

There is also a laptop-only RAG lab: `pip install 'labagent[rag-local]'`, then `labagent rag init` and `labagent rag ide`.

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
