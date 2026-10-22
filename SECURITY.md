# Security

`labagent` deletes cloud resources and handles credentials. Please report vulnerabilities privately through
GitHub's "Report a vulnerability" (Security tab), not in a public issue.

## Design guarantees (and how they are tested)

- Destructive actions are plan-then-apply with an integrity hash (`tests/test_plan.py`, `tests/test_sweep_moto.py`).
- Account allow-list is mandatory (`test_account_guard`, `test_empty_allowlist_refuses`).
- Passwords are only emitted RSA-OAEP encrypted (`tests/test_iam_ops.py`) and never enter Terraform state (the module declares no login profile; `tests/test_tf_wrapper.py`).
- State lives in a private, versioned, KMS-encrypted S3 bucket created by the packaged `state_backend` module (`labagent bootstrap-path`); no backend values are committed.
- Logs contain no emails (`test_logs_contain_no_emails`). The repo contains no PII or secrets (`scripts/check_no_pii.py`).

## Operating it safely

- Run it from a private repo using GitHub OIDC (see `docs/examples/`). Never from a public repo: run logs are public.
- Pin the commit you run (`LABAGENT_SHA`) and move the pin by reviewed PR.
- Use a dedicated AWS account for labs. Use separate read-only and write roles.
- Never commit rosters, credentials, Terraform state or job/result files. `.gitignore` blocks the common names.
