"""Configuration. No organization-specific value is hard-coded in the engine."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MODULE_DIR = Path(__file__).parent / "terraform" / "cohort_iam"
STATE_BACKEND_DIR = Path(__file__).parent / "terraform" / "state_backend"


def getenv(name: str) -> Optional[str]:
    """LABAGENT_<name>, falling back to the legacy LABCTL_<name> used before the rename."""
    return os.environ.get(f"LABAGENT_{name}") or os.environ.get(f"LABCTL_{name}") or None
# Keys from the earlier boto3-only design: accepted and ignored so old config files keep loading.
LEGACY_KEYS = {"group_name", "policy_name", "lambda_role_name", "policy_file"}


@dataclass
class Config:
    region: str = "us-east-1"
    # Regions scanned for regional resources. CloudFront (global) is always read via us-east-1.
    regions: List[str] = field(default_factory=lambda: ["us-east-1"])
    # Tag keys
    workshop_tag: str = "workshop"
    autodelete_tag: str = "autodelete"
    cohort_tag: str = "cohort"
    date_tag: str = "date"
    owner_tag_keys: Tuple[str, ...] = ("owner", "student", "learner_id")
    # The workshop tag VALUE that marks lab resources, e.g. "full-stack"
    workshop: str = "full-stack"
    # IAM naming: each cohort gets <name_prefix>-<cohort>-students / -sandbox / -lambda-exec
    name_prefix: str = "labs"
    student_domain: str = "example.com"
    # Initial console passwords: force a change at first sign-in? Set false when the LMS is the
    # place learners read their password, so the stored value stays valid.
    password_reset_required: bool = True
    # Terraform: binary (terraform or tofu) and the S3 backend. Values come from config/env, never from the repo.
    terraform_bin: Optional[str] = None
    backend: Dict[str, Any] = field(default_factory=dict)  # bucket, region, kms_key_id, use_lockfile, key_template
    # Safety
    allowed_account_ids: List[str] = field(default_factory=list)
    protected_name_prefixes: Tuple[str, ...] = (
        "aws-cloudtrail-logs-", "aws-config-", "cf-templates-", "elasticbeanstalk-",
        "amplify-", "cdk-", "codepipeline-", "do-not-delete-",
    )
    plan_max_age_s: int = 6 * 3600
    apply_time_budget_s: int = 1500
    password_length: int = 20

    def backend_settings(self) -> Dict[str, Any]:
        """Backend config with env overrides (so a private runner can set values as CI variables)."""
        b = {"key_template": "cohorts/{cohort}/terraform.tfstate", "use_lockfile": True, **self.backend}
        for env, key in (("TF_BACKEND_BUCKET", "bucket"), ("TF_BACKEND_REGION", "region"),
                         ("TF_BACKEND_KMS_KEY_ID", "kms_key_id")):
            if getenv(env):
                b[key] = getenv(env)
        return b


def load_config(path: Optional[str] = None) -> Config:
    """Load JSON config. Path from argument or LABAGENT_CONFIG; env LABAGENT_ALLOWED_ACCOUNTS overrides the allow-list."""
    path = path or getenv("CONFIG")
    raw = {}
    if path:
        raw = json.loads(Path(path).read_text())
    known = {f.name for f in fields(Config)}
    for k in LEGACY_KEYS & set(raw):
        raw.pop(k)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for k in ("protected_name_prefixes", "owner_tag_keys"):
        if k in raw:
            raw[k] = tuple(raw[k])
    cfg = Config(**raw)
    env_accounts = getenv("ALLOWED_ACCOUNTS")
    if env_accounts:
        cfg.allowed_account_ids = [a.strip() for a in env_accounts.split(",") if a.strip()]
    return cfg
