# Job and result contract

`labagent run --job job.json --output result.json` is the only entry point CI uses. The orchestrator
(any system that can issue the job and accept the callback) and `labagent` talk through two JSON documents.

## Job (orchestrator to labagent)

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | Echoed back in the result |
| `command` | `provision` `inventory` `sweep` `reset` `offboard` `audit` `cleanup` `launch` `destroy` | Anything else is refused |
| `mode` | `plan` `apply` `dry-run` | `sweep` needs `plan` or `apply`; `provision` supports `dry-run` |
| `cohort` | `{id, created_date}` | `created_date` is `dd-mmm-yyyy` and matches the `date` tag |
| `learners` | `[{learner_id, username, active?}]` | Roster. Held in memory and on the runner's ephemeral disk only |
| `plan` | object | Required for `sweep` + `apply`: the exact plan returned by a previous `plan` run |
| `approved_hash` | string | The plan hash a human approved. Apply is refused on mismatch |
| `secrets_pubkey_pem` | PEM | RSA public key. Required for `provision` and `reset` (passwords are RSA-OAEP SHA-256 encrypted to it) |
| `ruleset` | object | For `audit` and `cleanup` (plan). See docs/account-cleanup.md |
| `pack`, `name`, `params` | string, string, object | For `launch` and `destroy`: a pack id or alias, the instance name (`[a-z0-9-]`, 31 characters at most) and its parameters. `image` and `profile` are refused. These commands need no AWS account allow list: the pack brings its own credentials |
| `options` | object | `all_cohorts`, `force`, `learner_id` (reset), `only_learner_id` (sweep a single learner), `password_reset_required` (provision/reset; default from config), `regions` (audit/cleanup; `["*"]` = all enabled regions) |

## Result (labagent to orchestrator)

```json
{
  "run_id": "...", "command": "sweep", "mode": "plan", "cohort": "demo-cohort-1",
  "status": "ok | partial | failed",
  "summary": {}, "items": [{"arn":"","type":"","id":"","user":"learner_id","action":"delete|skip|create","reason":null,"state":"planned|deleted|pending|failed|gone|created|exists"}],
  "pending": [{"id":"","reason":"","retry_after_s":900}],
  "unattributed": [], "warnings": [],
  "plan": {"id":"","hash":"","created_at":0,"items":[]},
  "secrets": [{"user":"learner_id","kind":"console_password","ciphertext":"base64"}],
  "error": null
}
```

For `launch`, `secrets[]` holds one `ide_password` per learner, encrypted to `secrets_pubkey_pem` (also when the launch fails part way, so passwords already handed out are not lost). `summary.outputs` carries URLs and hosts only.

Exit code: 0 ok, 1 partial (something is `pending` or `failed`: re-run), 2 failed (nothing was done or the input was refused).

## Rules the engine enforces

- Refuses to run unless the current AWS account is in `allowed_account_ids`.
- `sweep` requires a cohort id, a cohort date, or an explicit `all_cohorts`.
- `apply` deletes only the items in the supplied plan. It never discovers anything.
- A plan is bound to its account, items and age (`plan_max_age_s`), and an optional approved hash.
- Resources tagged `autodelete=false`, with protected name prefixes, or of an unsupported type are skipped and reported, never deleted.
- Passwords exist in plaintext only inside the process, and leave it only as ciphertext.
- Logs (stderr) carry ids and counts only.
