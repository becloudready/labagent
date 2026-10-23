# labagent: Hands-on Labs CLI and AI Agent for RAG, Text-to-SQL, Databricks Lakebase and AWS Sandboxes

> An open-source **hands-on labs CLI and AI agent**. It launches **RAG** and **text-to-SQL** labs on **Databricks Lakebase** (Postgres with **pgvector**) and Databricks Apps, sets up **AWS training sandboxes with Terraform**, and removes everything when you are done. One command to launch a lab. One command to destroy it. A small local **Ollama** model can turn plain English into the right command.

[![PyPI](https://img.shields.io/pypi/v/labagent)](https://pypi.org/project/labagent/)
[![Python](https://img.shields.io/pypi/pyversions/labagent)](https://pypi.org/project/labagent/)
[![CI](https://github.com/becloudready/labagent/actions/workflows/ci.yml/badge.svg)](https://github.com/becloudready/labagent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Databricks Free Edition](https://img.shields.io/badge/Databricks-Free%20Edition-FF3621)](docs/packs.md)

**Related projects:** [DB Agent](https://github.com/db-agent/db-agent) (text-to-SQL agent) | [labagent-datasets](https://github.com/becloudready/labagent-datasets) (pinned lab datasets) | [BeCloudReady workshops](https://github.com/becloudready/workshops) (lab content)

---

## What is labagent?

labagent is a command line tool and AI agent for running hands-on technical labs and workshops. A lab is a **pack**: an ordered list of steps that is safe to re-run. `launch` builds the lab and stops with a clear error if something fails. Run it again and it resumes. `destroy` removes only what the lab created, so you do not pay for leftovers.

It covers three jobs:

- **RAG and text-to-SQL labs on Databricks.** Learners get a working retrieval-augmented generation app or a text-to-SQL agent in their own Databricks Free Edition workspace.
- **AWS training sandboxes.** Instructors create IAM users, a sandbox policy and a Lambda role per cohort with Terraform, then clean up afterwards.
- **A plain-English front end.** `labagent "deploy rag-sec on databricks"` shows the command it understood and asks before it runs anything.

---

## Features

- **Hands-on labs with no leftovers.** `labagent launch <lab>` builds the lab. `labagent destroy <lab>` removes it.
- **Safe to re-run.** Every step checks whether it is already done. Re-running resumes after a failure.
- **Real data, pinned.** The RAG lab loads SEC 10-K filings from a checksummed [dataset release](https://github.com/becloudready/labagent-datasets/releases/tag/sec-10k-2026.10). A download with a different SHA-256 is refused.
- **RAG with the parts that matter.** Section-aware chunking, hybrid search (pgvector plus Postgres full-text), metadata filters, cited answers and a retrieval quality check that must pass before the app deploys.
- **Text-to-SQL agent.** Deploys [db-agent](https://github.com/db-agent/db-agent) to Databricks Apps at a pinned commit, then asks a real test question through the live app.
- **Plain English with Ollama.** A small model (`qwen2.5-coder:0.5b` by default) picks the lab. Names and parameters are read from your sentence by exact rules, and you confirm before it runs.
- **AWS sandboxes with Terraform.** S3 state with KMS encryption, one state per cohort, learner passwords returned encrypted.
- **Safe cleanup of shared AWS accounts.** Plan first, then apply exactly that reviewed plan. Only an explicit `delete` rule can delete.
- **Your credentials stay yours.** Labs run on your machine with your Databricks CLI profile. The db-agent lab creates a 30-day token, stores it only as an app secret and revokes it on `destroy`.
- **Open source.** Apache-2.0, with a test suite of 170+ tests.

### Architecture

```
 labagent "deploy rag-sec"   -->  Ollama (qwen2.5-coder:0.5b)  -->  action + lab name
                                          |  you confirm the command
 labagent launch <lab>       -->  pack steps: preflight, build, load, index, evaluate, deploy
                                          |
                      Databricks CLI  -->  Lakebase (Postgres + pgvector), Databricks Apps, SQL warehouse

 labagent run  (CI or local) -->  AWS engine  -->  Terraform (cohort IAM) + boto3 (inventory, sweep, cleanup)
```

---

## Lab catalog

| Lab | What you learn | Cost | Command |
|---|---|---|---|
| `rag-sec-filings` | RAG over real SEC 10-K filings: chunking, hybrid search, citations, a quality gate before deploy | Databricks Free Edition | `labagent launch rag-sec-filings --name demo` |
| `rag-lakebase` | The same RAG pipeline on a small demo corpus, to smoke-test your setup | Databricks Free Edition | `labagent launch rag-lakebase --name demo` |
| `rag-sec-filings-live` | Fetch fresh filings from SEC EDGAR at launch | Databricks Free Edition | `labagent launch rag-sec-filings-live --name demo --param "user_agent=Your Name you@example.com"` |
| `db-agent` | Deploy a text-to-SQL agent over a Databricks sample dataset | Databricks Free Edition | `labagent launch db-agent --name demo` |

Free Edition limits apply: one Lakebase project, three apps, and apps stop after 24 hours. Details: [docs/packs.md](docs/packs.md).

## Deployment modes

| | Lab packs | AWS sandbox engine |
|---|---|---|
| **Best for** | Self-service learners, demos, workshop labs | Instructors running cohorts |
| **Runs** | On your machine | In a private CI repo with GitHub OIDC, or locally |
| **Targets** | Databricks: Lakebase, Apps, SQL warehouse | AWS: IAM, Lambda, and cleanup of untagged resources |
| **Auth** | Your Databricks CLI profile | OIDC, no static keys |
| **Cost** | Databricks Free Edition limits | Your AWS bill |

---

## Quick start: run your first hands-on lab

```bash
pip install 'labagent[rag]'          # Python 3.9+; labs use the Databricks CLI: databricks auth login
labagent packs                       # list the labs
labagent launch rag-sec-filings --name demo
labagent destroy rag-sec-filings --name demo
```

## Run a RAG lab on Databricks Lakebase (pgvector)

`rag-sec-filings` builds a retrieval-augmented generation (RAG) app on Postgres with pgvector. It loads a pinned snapshot of 10-K filings, scores retrieval on a set of questions, and deploys the app only if the score passes. The app has company and year filters, suggested questions and cited answers.

```bash
labagent launch rag-sec-filings --name demo
```

Data: 1,880 passages from 10 filings (AAPL, CVX, JPM, PFE, WMT). Known gaps, such as missing MD&A sections for JPM and CVX, are listed in the [dataset README](https://github.com/becloudready/labagent-datasets).

## Text-to-SQL agent on Databricks

`db-agent` clones [github.com/db-agent/db-agent](https://github.com/db-agent/db-agent) at a pinned commit and deploys it to Databricks Apps. It then asks a test question through the live app. The data is the `samples` catalog that every workspace has, for example `samples.bakehouse`.

```bash
labagent launch db-agent --name demo --param dataset=bakehouse
```

## Use labagent from plain English with Ollama

A small local model turns a sentence into a command. You see the command and confirm it before anything runs.

```bash
ollama pull qwen2.5-coder:0.5b
labagent "deploy rag-sec on databricks"
```

Any OpenAI-compatible endpoint works. See [docs/packs.md](docs/packs.md#plain-english).

## AWS training sandboxes with Terraform

labagent creates IAM users, a sandbox policy and a Lambda role per cohort with Terraform. Passwords are returned encrypted to your public key, so Terraform state holds no secrets. It also tears down what learners created.

| Command | What it does |
|---|---|
| `provision` | Create a cohort's IAM layer with Terraform |
| `inventory`, `sweep` | List and delete what learners created. Plan first, then apply that reviewed plan |
| `reset`, `offboard` | Rotate a password. Destroy a cohort |
| `audit`, `cleanup` | Clean a long-lived account from a ruleset |

```bash
labagent --config lab.json run --job job.json --output result.json
```

Job format: [docs/job-contract.md](docs/job-contract.md). State and secrets: [docs/state-and-secrets.md](docs/state-and-secrets.md).

## Clean up a shared AWS account safely

`audit` lists everything in an account. `cleanup` builds a plan from an explicit ruleset. You review the plan, then apply exactly that plan. A `keep` rule always wins, and anything that matches no rule is reported, never removed. See [docs/account-cleanup.md](docs/account-cleanup.md).

## Safety

- Plan first, then apply. The plan is bound to the account, its items, an age limit and the hash a human approved.
- Only a `delete` rule can delete. Unmatched resources are reported, never removed.
- labagent refuses to run in an AWS account that is not on your allow list.
- No plaintext secrets or emails in output or logs. No personal data in this repo. Tests enforce both.

---

## FAQ

**How do I deploy a RAG app on Databricks Free Edition?**
Run `pip install 'labagent[rag]'`, sign in with `databricks auth login`, then `labagent launch rag-sec-filings --name demo`.

**How do I remove everything a lab created?**
Run `labagent destroy <lab> --name demo`. It removes the app and its code. It also revokes the db-agent token and deletes the Lakebase project, but only when labagent created them.

**Do I need Ollama?**
No. Ollama is only for the plain-English front end. The explicit `launch` and `destroy` commands work without it.

**Does labagent store my Databricks credentials?**
No. Labs run with your Databricks CLI profile. The db-agent lab creates a 30-day token, stores it only as an app secret and revokes it on `destroy`.

**What does a lab cost?**
The Databricks labs target Free Edition, within its limits. The AWS sandbox engine uses your AWS account and bill.

**Can I add my own lab?**
Yes. Write a `Pack` class and register it through the `labagent.packs` entry point. See [docs/packs.md](docs/packs.md).

---

## Learning the concepts

The lab content that goes with these tools lives in the [BeCloudReady workshops](https://github.com/becloudready/workshops) repo.

| Workshop | What you build |
|---|---|
| [Databricks Genie and AI agents](https://github.com/becloudready/workshops/tree/master/workshops/databricks/genie-ai-agents/fundamentals) | LLM basics, structured output, tool use, the agentic loop and an MCP server |
| [Text-to-SQL on Databricks Lakebase](https://github.com/becloudready/workshops/tree/master/workshops/databricks/db-agent-lakebase) | Lakebase OLTP data, Unity Catalog gold tables and a self-hosted LLM endpoint |
| [AWS data lake](https://github.com/becloudready/workshops/tree/master/workshops/aws-data-lake) | S3, Glue, Athena, Lake Formation, Redshift Serverless, DMS and OpenSearch labs |
| [LLMOps](https://github.com/becloudready/workshops/tree/master/workshops/llmops) | GPU setup, vLLM, observability and an LLM gateway |

---

## Workshops and training

The team behind labagent teaches hands-on bootcamps on AI agents, RAG, text-to-SQL, Databricks and AWS. Running labs for a class or team? Private batches and a dashboard are available from [BeCloudReady](https://becloudready.com/). Community: [TorontoAI](https://toronto-ai.org/).

## Maintained by

[BeCloudReady](https://becloudready.com/), a Databricks Registered Partner. [Book a discovery call](https://calendly.com/kchandank/30-mins-meeting) for a customized training or consulting engagement.

Early release. Try labs in a throwaway workspace or AWS account first. Apache-2.0. See [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
