# Terraform state and secrets

## Who does what

| Layer | Tool | Why |
|---|---|---|
| Cohort IAM: users, group, sandbox policy, shared Lambda role | **Terraform** (`labagent/terraform/cohort_iam`) | Declarative, one state per cohort, `destroy` removes exactly that cohort |
| Console passwords | **labagent (IAM API)** | A Terraform login profile stores the password in state. Here state holds no secrets |
| Finding and deleting what learners created (Lambda, API Gateway, S3, CloudFront, EIP, ...) | **labagent sweeper** | Terraform cannot see resources it did not create |

`terraform` or `tofu` must be on the runner's PATH (or set `terraform_bin` / `LABAGENT_TF_BIN`). Terraform >= 1.11
(S3-native locking). OpenTofu is supported by the same HCL; OpenTofu additionally offers client-side state encryption
(`terraform { encryption { ... } }` with an AWS KMS key provider) if you want state that is ciphertext even to S3.

## One-time bootstrap (per AWS account)

```bash
cd "$(labagent bootstrap-path)"     # packaged with `pip install labagent`; copy it first if you want to keep its state
terraform init
terraform apply \
  -var bucket_name=<globally-unique-name> \
  -var 'admin_principal_arns=["<ARN of whoever runs this, from: aws sts get-caller-identity --query Arn>"]' \
  -var 'runner_principal_arns=["<ARN of the OIDC runner role>"]'
terraform output backend_config            # put these values in labagent's `backend` config
terraform output -raw runner_access_policy_json   # attach to the runner role
```

The bucket is private, versioned, SSE-KMS encrypted, TLS-only, and its policy denies every principal except the admin
and runner ARNs you list. **List yourself or you lock yourself out** (the root user can always repair the policy).
The bootstrap state is secret-free; keep it somewhere safe.

## How labagent uses it

- Per cohort state key: `cohorts/<cohort>/terraform.tfstate` (template configurable).
- `init -reconfigure -lockfile=readonly` with `-backend-config` flags for bucket, region, key, `encrypt=true`,
  `use_lockfile=true`, `kms_key_id`. Nothing organisation-specific is committed: values come from config or the
  `LABAGENT_TF_BACKEND_BUCKET`, `LABAGENT_TF_BACKEND_REGION`, `LABAGENT_TF_BACKEND_KMS_KEY_ID` environment variables.
- The roster is written to a 0600 var-file in a temp directory that is deleted afterwards. `TF_DATA_DIR` is outside
  the module directory.
- Terraform output is never logged. Errors are redacted (emails removed, short tail only).
- The provider lock file is committed with hashes for linux and darwin, and enforced with `-lockfile=readonly`.

## What is in state (and what is not)

In state: IAM user names (email-style, so treat as PII-adjacent), learner ids, policy and role definitions.
Not in state: passwords, access keys. The module declares no `aws_iam_user_login_profile`, and a test fails if one is added.
Protect the bucket accordingly. Keep PII out of git; S3 state in your own account is a different risk class.

## Passwords and the LMS

1. `provision` applies Terraform, then creates a login profile for each learner that has none and generates a password.
2. Each password is RSA-OAEP (SHA-256) encrypted to the orchestrator's public key and returned in `secrets[]`. Plaintext
   exists only inside the labagent process.
3. The orchestrator stores the ciphertext (for example in D1). It never holds plaintext at rest. To show a learner their
   password it decrypts with the private key (a Worker secret), over TLS, for the authenticated learner only, with
   `Cache-Control: no-store` and an audit entry per view.
4. `password_reset_required` (config, or per job via `options.password_reset_required`): if `true` the learner must
   change the password at first AWS sign-in, after which the stored value is stale. If the dashboard is meant to show
   a working password at any time, set it to `false` for that environment, and use `reset` to rotate on demand.
5. `reset` replaces the ciphertext. Delete ciphertext when the environment is offboarded.
