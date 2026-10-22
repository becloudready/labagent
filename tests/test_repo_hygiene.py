import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_pii_or_secrets_in_repo():
    p = subprocess.run([sys.executable, "scripts/check_no_pii.py"], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


SENSITIVE = [
    "priv.pem", "pub.pem", "server.key", "lab.json", "lab-prod.json", "job.json", "job-1.json", "result.json", "plan.json",
    "apply3.json", "audit2.json", "cleanup-plan.json", "terraform.tfstate", "terraform.tfstate.backup", "prod.tfvars",
    ".env", ".env.local", "students-credentials-1.csv", "roster-2026.csv", "roster.json", "rulesets/prod.json",
    "runs/plan.json", "local/lab.json", ".aws/credentials", "x.tfplan", "labagent/terraform/state_backend/terraform.tfstate",
    "labagent/terraform/cohort_iam/.terraform/terraform.tfstate", "ct.bin",
]
TRACKED = [
    "examples/job.provision.example.json", "examples/config.example.json", "examples/rulesets/legacy-cleanup.example.json",
    "docs/examples/oidc-trust-policy.example.json", "labagent/terraform/cohort_iam/.terraform.lock.hcl",
    "labagent/terraform/state_backend/.terraform.lock.hcl", ".env.example", "labagent/secrets_box.py", "pyproject.toml",
]


def _ignored(path: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0


def test_gitignore_blocks_every_sensitive_filename():
    leaked = [p for p in SENSITIVE if not _ignored(p)]
    assert not leaked, f"not ignored: {leaked}"


def test_gitignore_does_not_hide_files_that_must_be_committed():
    hidden = [p for p in TRACKED if _ignored(p)]
    assert not hidden, f"wrongly ignored: {hidden}"


def test_no_sensitive_file_is_in_the_repo_folder_or_candidate_set():
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split()
    bad = [f for f in files if f.endswith((".pem", ".key", ".tfstate", ".tfvars")) or Path(f).name in ("lab.json", "job.json", "result.json")]
    assert not bad, bad
