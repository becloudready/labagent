# labagent

Provision, audit and **safely tear down** ephemeral AWS sandboxes for workshops, bootcamps and shared training accounts.

```bash
pip install labagent     # Python 3.9+; Terraform or OpenTofu >= 1.11 for the cohort IAM layer
labagent --version
```

## Commands

| Command | What it does |
|---|---|
| `provision` | Terraform creates a cohort's IAM users, group, sandbox policy and Lambda role. Passwords are issued afterwards and returned **encrypted** to your public key, so state holds no secrets |
| `inventory`, `sweep` | List and delete what learners created. Plan first, then apply exactly that reviewed plan |
| `reset`, `offboard` | Rotate a learner's password; destroy a cohort (refuses while resources remain) |
| `labagent "deploy rag-sec"` | Plain-English front end: a small local model ([docs](docs/packs.md#plain-english)) |
| `launch`, `destroy` | Deploy a single lab from a pluggable **pack**, e.g. `rag-sec-filings` or [db-agent](https://github.com/db-agent/db-agent) ([docs](docs/packs.md)) |
| `audit`, `cleanup` | Clean an untagged, long-lived account from an explicit **ruleset** ([docs](docs/account-cleanup.md)) |

## Safety

- Plan, then apply: the plan is bound to the account, its items, an age limit and the hash a human approved.
- Only a `delete` rule can delete. `keep` rules always win; anything unmatched is reported, never removed.
- Refuses to run in an account that is not on your allow-list. Re-running is always safe.
- No plaintext secrets or emails in output or logs, and no PII in this repo (enforced by tests).

## Use

```bash
cp examples/config.example.json lab.json     # set allowed_account_ids and the S3 state backend
labagent --config lab.json run --job job.json --output result.json
```

Job and result formats: [docs/job-contract.md](docs/job-contract.md). State and secrets: [docs/state-and-secrets.md](docs/state-and-secrets.md).
Run it from a **private** CI repo with GitHub OIDC (examples in [docs/examples](docs/examples)), never from a public one.
Keep real configs, rosters, rulesets and job/result files out of this repo; `.gitignore` blocks the usual names.

Early release (v0.1), tested against mocked AWS: try a throwaway account first. Apache-2.0; see [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
