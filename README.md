# labagent

labagent is an open-source CLI that launches hands-on labs for RAG, LLM apps and Databricks, then removes them so you do not pay for leftovers. It also sets up and cleans AWS training sandboxes with Terraform.

## Quick start

```bash
pip install 'labagent[rag]'          # Python 3.9+; labs use the Databricks CLI: databricks auth login
labagent packs                       # list the labs
labagent launch rag-sec-filings --name demo
labagent destroy rag-sec-filings --name demo
```

Every step is safe to re-run. `launch` resumes where it stopped. `destroy` removes only what the lab created.

## Lab catalog

| Lab | What you learn | Cost | Command |
|---|---|---|---|
| `rag-sec-filings` | RAG over real SEC 10-K filings: chunking, hybrid search, citations, a quality gate before deploy | Databricks Free Edition | `labagent launch rag-sec-filings --name demo` |
| `rag-lakebase` | The same RAG pipeline on a small demo corpus, to smoke-test your setup | Databricks Free Edition | `labagent launch rag-lakebase --name demo` |
| `rag-sec-filings-live` | Fetch fresh filings from SEC EDGAR at launch | Databricks Free Edition | `labagent launch rag-sec-filings-live --name demo --param "user_agent=Your Name you@example.com"` |
| `db-agent` | Deploy [db-agent](https://github.com/db-agent/db-agent), a text-to-SQL agent, over a Databricks sample dataset | Databricks Free Edition | `labagent launch db-agent --name demo` |

Free Edition limits apply: one Lakebase project, three apps, and apps stop after 24 hours. Details: [docs/packs.md](docs/packs.md).

## Run a RAG lab on Databricks Lakebase

`rag-sec-filings` builds a RAG app on Postgres with pgvector. It loads a pinned snapshot of 10-K filings, runs a retrieval quality check, and deploys the app only if the check passes. The app has company and year filters, suggested questions and cited answers.

```bash
labagent launch rag-sec-filings --name demo
```

The data comes from a checksummed release in [labagent-datasets](https://github.com/becloudready/labagent-datasets). Known gaps are listed in its README.

## Text-to-SQL agent on Databricks

`db-agent` clones [github.com/db-agent/db-agent](https://github.com/db-agent/db-agent) at a pinned commit and deploys it to Databricks Apps. It then asks a test question through the live app. The data is the `samples` catalog every workspace has.

```bash
labagent launch db-agent --name demo --param dataset=bakehouse
```

## Use labagent from plain English

A small local model turns a sentence into a command. You see the command and confirm it before anything runs.

```bash
ollama pull qwen2.5-coder:0.5b
labagent "deploy rag-sec on databricks"
```

Any OpenAI-compatible endpoint works. See [docs/packs.md](docs/packs.md#plain-english).

## AWS sandboxes for workshops

labagent creates IAM users, a sandbox policy and a Lambda role per cohort with Terraform. Passwords are returned encrypted. It also tears down what learners created and cleans an untagged shared account from an explicit ruleset.

| Command | What it does |
|---|---|
| `provision` | Create a cohort's IAM layer with Terraform |
| `inventory`, `sweep` | List and delete what learners created. Plan first, then apply that reviewed plan |
| `reset`, `offboard` | Rotate a password. Destroy a cohort |
| `audit`, `cleanup` | Clean a long-lived account from a ruleset ([docs](docs/account-cleanup.md)) |

```bash
labagent --config lab.json run --job job.json --output result.json
```

Job format: [docs/job-contract.md](docs/job-contract.md). State and secrets: [docs/state-and-secrets.md](docs/state-and-secrets.md).

## Safety

- Plan first, then apply. The plan is bound to the account, its items, an age limit and the hash a human approved.
- Only a `delete` rule can delete. A `keep` rule always wins. Unmatched resources are reported, never removed.
- labagent refuses to run in an AWS account that is not on your allow list.
- No plaintext secrets or emails in output or logs. No personal data in this repo. Tests enforce both.

Early release. Try labs in a throwaway account or workspace first. Apache-2.0. See [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

Running labs for a class or team? Private batches and dashboard: https://becloudready.com
