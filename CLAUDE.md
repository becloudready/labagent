# labagent

Python CLI (Apache-2.0, published on PyPI, maintained by BeCloudReady) that builds and tears down hands-on labs. Each lab is a **pack**: ordered, idempotent steps behind `launch` and `destroy`. A second, older surface (`labagent run --job job.json`) manages AWS cohort sandboxes: Terraform IAM, a sweeper that deletes learner-created resources, audit and cleanup. A third, the GPU compute layer (`labagent/compute/`), rents GPU machines from several clouds and runs JupyterLab on them (packs `gpu-notebooks` and `gpu-notebooks-container`).

Public repo, untrusted PRs. Treat everything committed as public.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # pytest, moto, openpyxl, python-docx, build, twine
pytest -q                               # all tests, mocked (moto), no real cloud calls
pytest tests/test_redis_migrate.py -q   # one pack
python scripts/check_no_pii.py          # must pass before every push (also run as a test and in CI)
labagent packs [--json]                 # list packs
labagent launch <pack> --name demo --dry-run
labagent compute offers --gpu H100      # GPU machines you can rent now, every cloud with an API key, cheapest first (read only)
labagent status                         # machines launched from this computer (~/.labagent/instances.json)
```

CI (`.github/workflows/ci.yml`): Python 3.9 and 3.12, `check_no_pii.py`, `pytest -q`, plus a Terraform job that runs `fmt -check`, `validate` and `terraform test` on `labagent/terraform/{cohort_iam,state_backend,databricks_aws,databricks_azure}`. Code must stay Python 3.9 compatible (use `from __future__ import annotations`, `Optional[...]`, `Dict[...]`, no `match`, no `X | Y` at runtime).

## Layout

- `labagent/cli.py`: argparse entry. `labagent/run.py`: job runner (`COMMANDS`, `JOB_PARAM_DENY`, name regex). `labagent/nl.py`: plain-English front end (local Ollama or OpenAI-compatible endpoint; always confirms before running).
- `labagent/envelope.py`: the result contract. One JSON envelope on stdout/`--output`, `status` ok | partial | failed, exit code 0 | 1 | 2. Do not change its shape casually: an external orchestrator consumes it.
- `labagent/packs/base.py`: `Pack`, `Param`, `Step`, `PackContext`, `run_pack`, state files. `registry.py`: built-ins first, then `labagent.packs` entry points (a plugin can never shadow a built-in id). `REMOVED` maps retired pack ids to replacements.
- `labagent/compute/`: the cloud layer. `providers.py` has `Provider`, `Offer`, `HostSpec`, `Host` and the `PROVIDERS` registry (`shadeform`, `lambda`, `nebius`, `nebius-devlab`, `runpod`); one module per cloud. An `Offer.kind` is `vm`, `baremetal` or `container`. Provider flags decide what a pack may do: `container_only`, `has_ssh`, `builtin_jupyter`, `auto_delete`, `key_registry`. `cli.py` is `labagent compute ...`; `keys.py` makes the one shared SSH key.
- `labagent/instances.py`: `~/.labagent/instances.json` (0600, written by `launch`, removed by `destroy`) behind `labagent status`, `destroy NAME` and the `--cloud/--gpu/--ttl` launch flags. `labagent/console.py`: terminal colour, step progress and the quiet-by-default output (`-v` shows every detail line); pipes, files and CI always get the full plain output.
- `labagent/packs/<pack>/`: `gpu_notebooks` (`gpu-notebooks`: a VM or bare metal, one JupyterLab per MIG slice; `gpu-notebooks-container`: a container cloud such as RunPod or a Nebius Devlab, ONE JupyterLab on the cloud's own URL, nothing installed, never SSHes), `rag` (id `rag-sec-tutorial`), `db_agent`, `rag_ide`, `aml`, `dbx_workspace`, `dbx_lakehouse`, `azure_labs`, `redis_migrate`. Shared Databricks helpers in `packs/databricks/`.
- AWS engine at top level: `discovery.py`, `deleters.py` (REGISTRY with ordering), `sweep.py`, `cleanup.py`, `audit.py`, `iam_ops.py`, `rules.py`, `tf.py`, `secrets_box.py`, `config.py`, `context.py`.
- `labagent/terraform/`: packaged HCL (listed in `pyproject.toml` package-data). If you add a non-Python file that must ship, add its glob to `[tool.setuptools.package-data]` or it will be missing from the wheel (`tests/test_packaging.py` guards this).
- `docs/`: one guide per pack plus `packs.md`, `job-contract.md`, `state-and-secrets.md`. `README.md` is public and SEO-sensitive: keep edits deliberate and short.
- `tests/`: roughly one file per pack or module. `conftest.py` forces fake AWS creds and a moto account `123456789012`.

## Pack rules (from `packs/base.py`, enforce on every new or changed pack)

1. Every `Step` that can be re-run safely should have a `check`: cheap, read-only, returns True for "already done". Re-running `launch` resumes after failure.
2. Step `run` returns plain, **non-secret** outputs. They go into `ctx.state` and a 0600 JSON file at `~/.labagent/packs/<pack>/<name>.json` (`LABAGENT_HOME` overrides). Tokens, passwords and connections live only in `ctx.mem`.
3. `destroy` removes only what the pack created (tags or state). Never widen it to "everything that looks similar".
4. Failure messages from `PackError` are shown as-is, so keep them free of secrets. Unexpected exceptions are reported by class name only on purpose.
5. Real service clients come from `Pack.services(profile)`. Tests pass fake `services` into `run_pack`, so new external calls must go through `ctx.services` to stay testable without a cloud.
6. Register new packs in `registry.builtin()`, document in `docs/<pack>.md` and the README table, and add `tests/test_<pack>.py`.
7. `JOB_PARAM_DENY` (`image`, `profile`) blocks remote jobs from pointing at the runner's disk or identity. Keep sensitive params out of job-settable input.

## GPU compute layer

- A new cloud is a `Provider` subclass plus an entry in `PROVIDERS`. Tests drive it with a fake API object passed as `http=` (see `FakeRunpod`, `FakeNebius`, `FakeDevlabs` in `tests/test_runpod.py`, `tests/test_gpu_nebius.py`, `tests/test_nebius_devlab.py`): never a real cloud, never a real key.
- One JupyterLab per container, many (MIG slices) only on a VM or bare metal. Never install a second JupyterLab inside a container: use the cloud's own URL and password.
- A failed launch deletes the machine it rented. A cloud with no auto-delete (`auto_delete` False) bills until `labagent destroy`: say so in what the pack prints.
- API keys: environment variable, then `[cloud]` in `~/.labagent/credentials`, then for some clouds a file in `~/.labagent/keys/` (`docs/compute.md`). One SSH key serves every cloud: `labagent compute ssh-key-add --cloud all --name labagent`.
- Launching rents real machines and costs money. Do not run a real `launch` unless the owner asks; use `--dry-run`. Cloud facts in docs are dated: say what was verified on a real machine and what was not (`docs/gpu-notebooks.md` lists the unverified items).

## Safety gates

Cloud-touching packs refuse to run unless an allow-list env var is set (empty list means refuse):
`LABAGENT_ALLOWED_ACCOUNTS` (AWS), `LABAGENT_ALLOWED_SUBSCRIPTIONS` (Databricks on Azure), `LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS` (azure-labs). Do not weaken or bypass these, and do not set them on the user's behalf. `azure-labs` and `redis-migrate` treat the source as read-only. Never run `launch`/`destroy` against a real account or workspace unless the user asks; default to `--dry-run` and mocks.

## Public-repo hygiene

- No organisation-specific values in code, tests, examples or docs: use `example.com` and AWS account `123456789012`. `scripts/check_no_pii.py` fails on real emails, 12-digit account IDs, UUIDs and key material in tracked or untracked-not-ignored files.
- `tests/test_repo_hygiene.py` asserts `.gitignore` blocks state, tfvars, `.env`, key and credentials filenames and still lets the `*.example.json`, lock files and `secrets_box.py` be tracked. Update its lists when adding such files.
- Terraform state must not hold secrets. Console passwords are set through the IAM API and sealed to the orchestrator's public key (`secrets_box.py`), not through Terraform login profiles.
- Logs go to stderr and contain ids and counts only.

## Releasing

- Bump `__version__` in `labagent/__init__.py` (check the current value and `git tag` first). **Patch bumps only** (0.4.2, 0.4.3, ...), no minor jumps unless the owner says so.
- Merge to `main`, then tag `vX.Y.Z`. The `release` workflow builds, runs `twine check --strict`, checks the tag equals the version, and publishes with PyPI Trusted Publishing (no tokens). Use the manual `testpypi` run first for a dry run. Never publish by hand.
- `labagent/`, `scripts/` and `.github/` changes need maintainer review (CODEOWNERS).

## Working conventions

- Make a backup or work on a branch before large edits. Commits and PRs only when asked. Features land through PRs referencing issues (for example "part of #36" for redis-migrate).
- Pinned third-party sources (db-agent commit, SEC dataset with SHA-256) are pinned deliberately. Bump a pin only as its own change, and update the docs that describe what it picks (`docs/packs.md`).
- Ask before changing the envelope schema, pack ids or aliases, or `REMOVED`: they are public interfaces.
- Related repos: `db-agent/db-agent` (text-to-SQL app deployed by the `db-agent` pack), `becloudready/labagent-datasets` (datasets).

## Known gaps (check the README and docs for the current state)

Verified on real services: Databricks Free Edition labs, the AWS premium workspace, azure-labs copy and delete. Mock-only so far: `dbx-workspace` on Azure and custom network, `dbx-lakehouse` on AWS and Azure. `redis-migrate` is read-only check only; export and import are not built yet. GPU layer: Shadeform VMs were launched for real; RunPod pod creation and SSH were verified, but RunPod's Jupyter login with labagent's password, a Lambda launch and a Nebius Devlab reaching RUNNING are not.
