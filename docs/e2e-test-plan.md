# End-to-end test plan: onboarding a cohort of 10

Use a **throwaway or dedicated AWS account**, never the shared one. Cost: 10 IAM users are free; test resources cost cents.
Put a $20 AWS Budget alarm on the account first.

All fake data: `e2e-student-01..10@example.com`, cohort `e2e-001`, date `01-Jan-2030`.

## Stage A: engine only (no GitHub, no LMS). About 2 hours. Can be done today.

Setup
```bash
cd labagent && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && source .venv/bin/activate
cp examples/config.example.json lab.json        # set allowed_account_ids to the TEST account id; keep lab.json out of git
export AWS_PROFILE=<admin profile for the test account>
openssl genrsa -out priv.pem 2048 && openssl rsa -in priv.pem -pubout -out pub.pem
```

### A0: bootstrap the state backend (once per account)

Follow `docs/state-and-secrets.md`: run the state backend module (`cd "$(labagent bootstrap-path)"`) with your own caller ARN in `admin_principal_arns` and
`runner_principal_arns`, then copy `terraform output backend_config` into `lab.json` under `"backend"`.
Also set `"name_prefix": "e2e"` so the test group, policy and role are `e2e-e2e-001-students` etc. and cannot collide with
anything real. Terraform >= 1.11 must be installed (`terraform version`).

| # | Action | Expected |
|---|---|---|
| A1 | `python scripts/make_test_job.py --command provision --mode dry-run --cohort e2e-001 --date 01-Jan-2030 --pubkey pub.pem > job.json`, then `labagent --config lab.json run --job job.json --output result.json` | exit 0, 10 items `planned` (a real `terraform plan`, read-only: no lock, no state write), nothing in IAM |
| A2 | Same with no `--mode` (apply) | exit 0, 10 `created`, 10 `secrets` with ciphertext only. IAM shows 10 users, the cohort group, policy and Lambda role. S3 shows `cohorts/e2e-001/terraform.tfstate`: download it and confirm it contains **no password** |
| A3 | Decrypt one: `jq -r '.secrets[0].ciphertext' result.json \| base64 -d > ct.bin; openssl pkeyutl -decrypt -inkey priv.pem -in ct.bin -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256` | prints a 20 char password. `grep` the result file for that password: no match |
| A4 | Re-run A2 | 10 `exists`, **0 secrets** (no password re-issued), exit 0 |
| A5 | Sign in to the console as student 01 with the decrypted password | forced to set a new password. Then: create an S3 bucket and a t3.micro EC2 (allowed); try us-west-2 (denied); try an m5.large (denied by the new instance-type rule, which is untested on real AWS) |
| A6 | As students 01 and 02, create resources named `student-e2e-student-01-*`: a Lambda and an S3 bucket, tagged `workshop=full-stack`, `autodelete=true`, `date=01-Jan-2030`. Add: one tagged `autodelete=false`, one with **no tags**, and an Elastic IP tagged like the others | all created |
| A7 | `make_test_job.py --command inventory ...` and run | per-learner counts for 01 and 02, the EIP present, untagged resource absent (known gap W4), exit 0 |
| A8 | `--command sweep --mode plan`, run | read-only (nothing deleted). `items` show `delete` for tagged resources, `skip` with reason for the `autodelete=false` one. Save `result.json` |
| A9 | Tamper: change one `skip` to `delete` in the plan, run `sweep --mode apply` | **refused**, exit 2, `plan hash mismatch`, nothing deleted |
| A10 | Apply the untouched plan with `--approved-hash <plan.hash>` | tagged resources gone, `autodelete=false` and untagged remain, exit 0 |
| A11 | Create a resource after the plan, apply the same plan again | the new resource survives (apply never rediscovers) |
| A12 | `reset --learner-id 3` | new ciphertext, old password stops working |
| A13 | Account guard: put a wrong id in `allowed_account_ids`, run any command | exit 2, refuses |
| A14 | Leave one tagged resource, run `offboard` | **refused**. Sweep it, run `offboard` again | 10 users, the cohort group, policy and Lambda role deleted by `terraform destroy`; the state bucket remains |
| A15 | Run offboard once more | exit 0, no changes (idempotent) |
| A16 | Run two provision jobs at the same time for the same cohort | the second waits for the S3 lock (up to 5 min) or fails cleanly; state is not corrupted |

CloudFront: create one distribution tagged as above, plan, apply, expect `pending` ("disabling"), re-run after about 15 to 30 minutes, expect `deleted`. REST API: create 3, expect throttled waits then `deleted` or `pending` with retry time.

## Stage B: runner repo with a stand-in orchestrator. About half a day, needs lms-new L1 to L3.

Prerequisites: private `labagent-runner` repo with `lab-job.yml` from `docs/examples/`, `config/lab.json`, the four repo variables; AWS OIDC provider and the two roles (trust policy pinned to repo and `main`); lms-new dev worker with the job and callback endpoints, the RSA private key, and a GitHub token limited to the runner repo.

| # | Check | Expected |
|---|---|---|
| B1 | Dispatch a provision run from the Worker | workflow starts, fetches job, assumes the **write** role, posts result; run `success` |
| B2 | Dispatch a sweep **plan** | assumes the **read** role (confirm in the run log) |
| B3 | Fetch the same job twice | second fetch rejected |
| B4 | Call the job endpoint with a token from a different repo or branch | rejected (`repository`, `ref` claims) |
| B5 | Post the callback twice | second is a no-op (idempotent by run id) |
| B6 | Dispatch two runs at once | they serialise (concurrency group) |
| B7 | Read the run log and download any artifacts | **no emails, no passwords**, no artifacts; `job.json`/`result.json` removed |
| B8 | Apply with a plan whose hash was not approved | refused |
| B9 | Break `LABAGENT_SHA` to a wrong value | job fails before AWS is touched |

## Stage C: the 10-student dashboard flow. About half a day, needs lms-new L4 to L10.

1. Import 10 learners into a batch (bulk import).
2. Create the lab environment, run **provision dry-run**, review, approve, apply.
3. Each learner opens "My Lab": sees console URL and username, reveals the password **once**; a second reveal is blocked; the ciphertext is purged after the retention period.
4. Three learners really sign in and deploy a project; one hits a problem and uses reset.
5. Inventory view: per-learner counts, unattributed list.
6. Environment end date: sweep plan appears for approval, admin approves, apply runs, pending CloudFront items are retried by the dashboard, then offboard.
7. Audit log has one entry per action with actor and plan hash. Trainer role can view but not approve.

## Pass criteria

- Zero plaintext passwords anywhere (results, logs, artifacts, D1 rows).
- Zero resources deleted that were not in the approved plan.
- Account is clean afterwards: no users, no tagged resources, no Elastic IPs, no CloudFront.
- Every step is re-runnable without harm.

## Known gaps to expect

Untagged resources are not found (W4); no cost report; ECR, NAT, load balancers and IAM roles are not swept.
