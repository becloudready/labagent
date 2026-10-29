"""Who is about to pay for this? Check the cloud login and refuse any account or subscription that is not on the allow list."""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...config import getenv
from ..base import PackError

GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def allowed(name: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    raw = (env or os.environ).get(f"LABAGENT_{name}") or getenv(name) or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


class AwsAuth:
    """Real AWS check. Imported lazily; tests pass their own."""

    def __init__(self, profile: Optional[str] = None, region: Optional[str] = None):
        self.profile, self.region = profile, region

    def check(self) -> str:
        import boto3
        try:
            who = boto3.Session(profile_name=self.profile, region_name=self.region).client("sts").get_caller_identity()
        except Exception as e:
            raise PackError(f"could not read AWS credentials ({type(e).__name__}). Configure them, or pass --param profile=<name>") from e
        return who["Account"]


class AzureAuth:
    """Reads the active subscription from `az account show`, or from the service principal variables."""

    def __init__(self, run: Callable[..., Any] = subprocess.run, env: Optional[Dict[str, str]] = None):
        self._run, self.env = run, dict(os.environ if env is None else env)

    def check(self) -> Tuple[str, str]:
        e = self.env
        if all(e.get(k) for k in ("ARM_CLIENT_ID", "ARM_CLIENT_SECRET", "ARM_TENANT_ID", "ARM_SUBSCRIPTION_ID")):
            return e["ARM_SUBSCRIPTION_ID"], "service principal"
        try:
            p = self._run(["az", "account", "show", "--output", "json"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=e)
        except FileNotFoundError:
            raise PackError("the Azure CLI was not found. Install it and run: az login   (or set the ARM_* service principal variables)") from None
        if p.returncode != 0:
            raise PackError("not signed in to Azure. Run: az login   (or set ARM_CLIENT_ID, ARM_CLIENT_SECRET, ARM_TENANT_ID and ARM_SUBSCRIPTION_ID)")
        info = json.loads(p.stdout)
        return info["id"], "az login"


def require_databricks_account(env: Dict[str, str], account_id: Optional[str]) -> str:
    """AWS only. Account-level OAuth (a service principal's client id and secret) from the environment; never a parameter."""
    acct = account_id or env.get("DATABRICKS_ACCOUNT_ID")
    if not acct or not GUID.match(acct):
        raise PackError("the Databricks account ID is missing or is not a GUID. Pass --param account_id=<id> or set DATABRICKS_ACCOUNT_ID "
                        "(account console, top right corner)")
    missing = [k for k in ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET") if not env.get(k)]
    if missing:
        raise PackError(f"set {' and '.join(missing)} in the environment: the OAuth credentials of an account-level service principal. "
                        "They are read by Terraform and are never written to a file or printed")
    return acct
