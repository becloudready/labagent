"""dbx-workspace: create a premium Databricks workspace on AWS or Azure with Terraform, and remove it again.

Plan first. Nothing is created or deleted unless you pass `--param apply=yes`. Without it the pack runs `terraform plan`, reports what it
would do (counts only) and stops. With it, the pack applies that saved plan.

Azure: the workspace SKU is the literal "premium" in the module.
AWS:   Terraform cannot buy or change a Databricks plan. The tier comes from the Databricks account. The pack reads the tier the new
       workspace reports and fails if it is not PREMIUM or ENTERPRISE.

Credentials are never parameters. They come from the environment and the cloud CLIs, and Terraform reads them there.
"""
from __future__ import annotations

import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Pack, PackContext, PackError, Param, Step, save_state, state_dir
from . import guards
from .tf import Tf

DEFAULT_REGION = {"aws": "us-east-1", "azure": "eastus2"}
PREMIUM_TIERS = {"PREMIUM", "ENTERPRISE"}
COST_NOTE = {
    "aws": ("A workspace itself has no hourly infrastructure in your AWS account. network=custom adds a NAT gateway and interface endpoints, "
            "which AWS bills by the hour. Clusters and SQL warehouses cost money while they run."),
    "azure": "The workspace has no standing infrastructure cost beyond what you start. Clusters and SQL warehouses cost money while they run.",
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:24]


class DatabricksWorkspace(Pack):
    id = "dbx-workspace"
    title = "Premium Databricks workspace on AWS or Azure (Terraform)"
    summary = "Creates a premium Databricks workspace in your AWS or Azure account with Terraform, plan first, and removes it again"
    aliases = ("databricks-workspace", "premium-workspace")
    platforms = ("aws", "azure")
    cost_tier = "metered"
    default_name = "dbx"
    output_keys = ("cloud", "region", "workspace_url", "workspace_id", "tier", "network", "status", "message", "plan", "resources", "state_file", "cost_note")
    params = [
        Param("cloud", None, "aws | azure", required=True),
        Param("region", None, "AWS region (default us-east-1) or Azure location (default eastus2)"),
        Param("network", "managed", "managed (the cloud or Databricks creates the network) | custom (Terraform creates a VPC on AWS, or a VNet on Azure)"),
        Param("apply", "no", "no: run terraform plan and stop | yes: create (or, for destroy, delete) for real"),
        Param("account_id", None, "AWS: the Databricks account ID (or DATABRICKS_ACCOUNT_ID). Not a secret"),
        Param("subscription_id", None, "Azure: subscription ID (default: the active az login subscription)"),
        Param("profile", None, "AWS: profile name for the AWS credentials"),
        Param("hardened", "no", "Azure premium extras: infrastructure encryption and automatic cluster updates (yes | no)"),
        Param("owner", "labagent", "Owner tag. Use a team name, not a personal email"),
        Param("admin_emails", None, "AWS: comma-separated emails of existing Databricks account users to give access to the workspace"),
        Param("admin_role", "ADMIN", "AWS: ADMIN | USER, the workspace role given to every address in admin_emails"),
        Param("force_destroy", "no", "AWS destroy: also delete the root bucket if it holds workspace files (yes | no)"),
        Param("backend", "local", "Terraform state: local | s3 | azurerm. Local state is lost with this folder: use s3 or azurerm for anything you keep"),
        Param("state_bucket", None, "backend=s3: bucket name (the key is dbx-workspace/<name>.tfstate)"),
        Param("state_storage", None, "backend=azurerm: <resource group>/<storage account>/<container>"),
    ]

    # ---- helpers ---------------------------------------------------------------------------------------------------------------
    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {}

    def work_dir(self, ctx: PackContext) -> Path:
        return state_dir() / "dbx-workspace" / f"{ctx.name}-tf"

    def _check(self, ctx: PackContext) -> Dict[str, Any]:
        p = ctx.params
        cloud = str(p["cloud"] or "").lower()
        if cloud not in ("aws", "azure"):
            raise PackError("cloud must be aws or azure")
        prefix = slug(ctx.name)
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,23}", prefix):
            raise PackError("the instance name must be 3 to 24 letters, digits or hyphens, starting with a letter (it names every resource)")
        if p["network"] not in ("managed", "custom"):
            raise PackError("network must be managed or custom")
        for k in ("apply", "hardened", "force_destroy"):
            if str(p[k]).lower() not in ("yes", "no"):
                raise PackError(f"{k} must be yes or no")
        if p["backend"] not in ("local", "s3", "azurerm"):
            raise PackError("backend must be local, s3 or azurerm")
        if cloud == "aws" and str(p["hardened"]).lower() == "yes":
            raise PackError("hardened applies to Azure only")
        if cloud == "azure" and str(p["force_destroy"]).lower() == "yes":
            raise PackError("force_destroy applies to AWS only")
        people = self._people(p["admin_emails"])
        if people and cloud != "aws":
            raise PackError("admin_emails applies to AWS only")
        if str(p["admin_role"]).upper() not in ("ADMIN", "USER"):
            raise PackError("admin_role must be ADMIN or USER")
        if str(p["owner"]).count("@"):
            raise PackError("owner is stored as a cloud tag: use a team name, not an email address")
        return {"cloud": cloud, "prefix": prefix, "region": p["region"] or DEFAULT_REGION[cloud]}

    @staticmethod
    def _people(raw: Any) -> List[str]:
        out = [e.strip() for e in str(raw or "").split(",") if e.strip()]
        bad = [e for e in out if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)]
        if bad:
            raise PackError(f"admin_emails has {len(bad)} entry that is not an email address. Separate addresses with commas")
        return sorted(set(out))

    def _backend(self, ctx: PackContext, cloud: str, region: str) -> Dict[str, Any]:
        kind = ctx.params["backend"]
        if kind == "local":
            path = self.work_dir(ctx).parent / f"{ctx.name}.tfstate"
            return {"type": "local", "config": {"path": str(path)}}
        if kind == "s3":
            if not ctx.params["state_bucket"]:
                raise PackError("backend=s3 needs --param state_bucket=<bucket>")
            return {"type": "s3", "config": {"bucket": ctx.params["state_bucket"], "key": f"dbx-workspace/{ctx.name}.tfstate", "region": region,
                                              "encrypt": "true", "use_lockfile": "true"}}
        parts = str(ctx.params["state_storage"] or "").split("/")
        if len(parts) != 3 or not all(parts):
            raise PackError("backend=azurerm needs --param state_storage=<resource group>/<storage account>/<container>")
        return {"type": "azurerm", "config": {"resource_group_name": parts[0], "storage_account_name": parts[1], "container_name": parts[2],
                                              "key": f"dbx-workspace/{ctx.name}.tfstate"}}

    def _variables(self, ctx: PackContext, base: Dict[str, Any]) -> Dict[str, Any]:
        p, cloud = ctx.params, base["cloud"]
        net = "managed" if p["network"] == "managed" else ("vpc" if cloud == "aws" else "vnet")
        v: Dict[str, Any] = {"prefix": base["prefix"], "network": net, "owner": p["owner"]}
        if cloud == "aws":
            v.update(databricks_account_id=ctx.state["account_id"], region=base["region"], force_destroy=str(p["force_destroy"]).lower() == "yes")
            if self._people(p["admin_emails"]):
                v.update(admin_emails=self._people(p["admin_emails"]), admin_role=str(p["admin_role"]).upper())
        else:
            v.update(subscription_id=ctx.state["subscription_id"], location=base["region"], hardened=str(p["hardened"]).lower() == "yes")
        return v

    def _tf(self, ctx: PackContext) -> Tf:
        made = ctx.services.get("tf")
        return made(self.work_dir(ctx)) if made else Tf(self.work_dir(ctx), env=ctx.services.get("env"))

    def _env(self, ctx: PackContext) -> Dict[str, str]:
        import os
        return dict(ctx.services.get("env") if ctx.services.get("env") is not None else os.environ)

    # ---- launch ----------------------------------------------------------------------------------------------------------------
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        return self._steps(ctx, destroy=False)

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        return self._steps(ctx, destroy=True)

    def _steps(self, ctx: PackContext, destroy: bool) -> List[Step]:
        yes = str(ctx.params.get("apply", "no")).lower() == "yes"

        def preflight(c: PackContext):
            base = self._check(c)
            env = self._env(c)
            out: Dict[str, Any] = {"cloud": base["cloud"], "region": base["region"], "network": c.params["network"]}
            if base["cloud"] == "aws":
                acct = guards.require_databricks_account(env, c.params["account_id"])
                aws = c.services.get("aws") or guards.AwsAuth(c.params["profile"], base["region"])
                aws_account = aws.check()
                ok = guards.allowed("ALLOWED_ACCOUNTS", env)
                if not ok:
                    raise PackError("LABAGENT_ALLOWED_ACCOUNTS is empty: refusing to create or delete anything. Set it to your AWS account ID")
                if aws_account not in ok:
                    raise PackError("the current AWS account is not in LABAGENT_ALLOWED_ACCOUNTS: refusing to run")
                out.update(account_id=acct)
            else:
                az = c.services.get("azure") or guards.AzureAuth(env=env)
                sub, how = az.check()
                if c.params["subscription_id"] and c.params["subscription_id"] != sub:
                    raise PackError("--param subscription_id does not match the active Azure subscription. Run: az account set --subscription <id>")
                ok = guards.allowed("ALLOWED_SUBSCRIPTIONS", env)
                if not ok:
                    raise PackError("LABAGENT_ALLOWED_SUBSCRIPTIONS is empty: refusing to create or delete anything. Set it to your subscription ID")
                if sub not in ok:
                    raise PackError("the active Azure subscription is not in LABAGENT_ALLOWED_SUBSCRIPTIONS: refusing to run")
                if not guards.GUID.match(sub):
                    raise PackError("the subscription ID is not a GUID")
                out.update(subscription_id=sub)
            if destroy and not c.state.get("applied"):
                raise PackError("nothing to destroy: this name has no workspace in the pack's records. (If a run failed half way, re-run launch first.)")
            self._tf(c).binary()
            return out

        def prepare(c: PackContext):
            base = self._check(c)
            base["region"] = c.state["region"]
            tf = self._tf(c)
            backend = self._backend(c, base["cloud"], base["region"])
            if backend["type"] == "local":
                folder = Path(backend["config"]["path"]).parent
                folder.mkdir(parents=True, exist_ok=True)
                folder.chmod(0o700)
            tf.prepare(base["cloud"], self._variables(c, base), backend)
            tf.init()
            return {"backend": backend["type"], "state_file": backend["config"].get("path", "") if backend["type"] == "local" else ""}

        def plan(c: PackContext):
            summary = self._tf(c).plan(destroy=destroy)
            c.log(f"  plan: {summary['add']} to add, {summary['change']} to change, {summary['destroy']} to destroy, {summary['replace']} to replace")
            if not destroy and summary["destroy"] + summary["replace"] > 0:
                raise PackError(f"the plan would delete or replace {summary['destroy'] + summary['replace']} resource(s). A workspace is replaced when its network or region "
                                "changes, and that loses its notebooks. Nothing was changed. To start over, destroy it and create it again on purpose")
            return {"plan": summary}

        def apply(c: PackContext):
            n = c.state["plan"]
            if not yes and destroy:
                # A failure on purpose: the framework forgets a pack's records after a successful destroy, and nothing was destroyed.
                raise PackError(f"Preview only: {n['destroy']} resources would be deleted, including the workspace and its notebooks. "
                                "Nothing was deleted. Run again with --param apply=yes to delete them.")
            if not yes:
                return {"status": "planned", "applied": c.state.get("applied", False),
                        "message": f"Nothing was created: {n['add']} resources would be added. Review the plan, then run again with --param apply=yes to create them."}
            tf = self._tf(c)
            if not destroy:
                c.state["applied"] = True              # recorded before the first change, so a half-finished apply can still be destroyed
                save_state(c.pack_id, c.name, c.state)
            tf.apply_plan()
            if destroy:
                return {"status": "destroyed", "applied": False}
            return {"status": "created", "applied": True}

        def verify(c: PackContext):
            if not yes or destroy:
                return {}
            tf, cloud = self._tf(c), c.state["cloud"]
            o = tf.outputs()
            url = str(o.get("workspace_url") or "")
            if cloud == "azure":
                tier = str(o.get("sku") or "")
                if tier.lower() != "premium":
                    raise PackError(f"the workspace reports the {tier or 'unknown'} SKU, not premium. Destroy it and check the module")
                url = f"https://{url}" if url and not url.startswith("http") else url
            else:
                tier = str(o.get("pricing_tier") or "")
                if tier and tier.upper() not in PREMIUM_TIERS:
                    raise PackError(f"the workspace was created but reports the {tier} tier, not PREMIUM. Your Databricks account plan decides the tier on AWS. "
                                    "Upgrade the plan, or remove this workspace with: labagent destroy dbx-workspace --param apply=yes")
            note = None
            if url:
                try:
                    (c.services.get("probe") or probe)(url)
                except Exception as e:                 # a slow first start is normal: report it, do not fail
                    note = f"the workspace URL did not answer yet ({type(e).__name__}); try again in a few minutes"
            res = {"workspace_url": url, "workspace_id": str(o.get("workspace_id") or ""), "tier": tier.upper() or "not reported",
                   "cost_note": COST_NOTE[cloud]}
            if note:
                res["status"] = "created (waiting for the URL)"
            return res

        def cleanup(c: PackContext):
            if yes and destroy:
                shutil.rmtree(self.work_dir(c), ignore_errors=True)
                st = self.work_dir(c).parent / f"{c.name}.tfstate"
                for suffix in ("", ".backup"):
                    Path(str(st) + suffix).unlink(missing_ok=True)
            elif yes:
                (self.work_dir(c) / "plan.bin").unlink(missing_ok=True)
            return {}

        return [
            Step("preflight", "Check the login, the allow list and the options", preflight),
            Step("prepare", "Copy the Terraform module and initialise it", prepare),
            Step("plan", "Run terraform plan" + (" -destroy" if destroy else ""), plan),
            Step("apply", ("Delete" if destroy else "Create") + " the workspace (only with apply=yes)", apply),
            Step("verify", "Check the tier and the workspace URL", verify),
            Step("cleanup", "Remove temporary files", cleanup),
        ]

    def outputs(self, state: Dict[str, Any]) -> Dict[str, Any]:
        o = {k: v for k, v in state.items() if k in self.output_keys}
        if "plan" in o:
            o["resources"] = o["plan"].get("types", {})
        return o


def probe(url: str) -> None:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "labagent"})
    with urllib.request.urlopen(req, timeout=10):
        pass
