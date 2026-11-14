# Job and result contract

`labagent run --job job.json --output result.json` is the only entry point CI uses. The orchestrator
(any system that can issue the job and accept the callback) and `labagent` talk through two JSON documents.

By hand, the job, the `--config` file and the `--ruleset` file may be YAML instead: name them `.yaml` or `.yml`. They read exactly as the same JSON would (a number stays a number, no value is expanded), with three guards: no aliases (`*name`), no repeated keys, and `007` or `0x10` stay text. **Quote** an id that is all digits, such as `"1"` or an AWS account id: unquoted, it is a number. The result is always JSON. `examples/` has a commented file of each kind.

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
| `pack`, `name`, `params` | string, string, object | For `launch` and `destroy`: a pack id or alias, the instance name (`[a-z0-9-]`, 31 characters at most) and its parameters. `image`, `profile`, `ssh_key_file` and `keep_on_failure` are refused (`ssh_key_file` would point labagent at a file on the machine that runs the job, `keep_on_failure` would leave a billed VM running; the runner's owner sets the key file with `LABAGENT_SSH_KEY_FILE`). These commands need no AWS account allow list: the pack brings its own credentials |
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

For `launch`, `secrets[]` holds one `ide_password` per learner, encrypted to `secrets_pubkey_pem` (also when the launch fails part way, so passwords already handed out are not lost). `summary.outputs` carries URLs and hosts only. When a `launch` fails, `summary.outputs` still carries what was learned before it failed (for gpu-notebooks, which SSH key was used and whether it matched, and `auto_delete`: `false` when the cloud, such as Lambda, will not delete the VM by itself, so only `destroy` stops the billing), and `summary.cleanup` says what was done about anything billable the launch had rented: `{"action": "deleted" | "kept" | "nothing" | "failed", "host_id": "...", "detail": "..."}`. A caller treats `failed` as an alarm: a VM may still be running.

Exit code: 0 ok, 1 partial (something is `pending` or `failed`: re-run), 2 failed (nothing was done or the input was refused).

## Rules the engine enforces

- Refuses to run unless the current AWS account is in `allowed_account_ids`.
- `sweep` requires a cohort id, a cohort date, or an explicit `all_cohorts`.
- `apply` deletes only the items in the supplied plan. It never discovers anything.
- A plan is bound to its account, items and age (`plan_max_age_s`), and an optional approved hash.
- Resources tagged `autodelete=false`, with protected name prefixes, or of an unsupported type are skipped and reported, never deleted.
- Passwords exist in plaintext only inside the process, and leave it only as ciphertext.
- Logs (stderr) carry ids and counts only.
