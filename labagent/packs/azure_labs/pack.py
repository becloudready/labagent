"""azure-labs: rebuild the Azure Lab Services classroom workflow on standard Azure VMs.

This is an alternative to Azure Lab Services, simulated with ordinary VMs. It is not Microsoft's service.
This first version creates the lab's resource group and network and tears them down by tag. Later steps add the template
image, student VMs, shutdown schedules, reset and the browser gateway.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ..base import Pack, PackContext, PackError, Param, Step
from . import lifecycle, safety
from .azcli import AzCli

ARM_DIR = Path(__file__).parent / "arm"


class _LazyAz:
    """Creates the real `az` wrapper on first use, inside a step, so `--dry-run` works without Azure installed."""

    def __init__(self) -> None:
        self._az = None
        self.subscription = None

    def __getattr__(self, name: str):
        if self._az is None:
            self._az = AzCli(self.subscription)
        return getattr(self._az, name)


class AzureLabs(Pack):
    id = safety.PACK_ID
    title = "Azure Labs: classroom VMs, an Azure Lab Services alternative"
    summary = "Rebuild an Azure Lab Services class on standard Azure VMs, one per student"
    aliases = ("azure-lab", "als-migration")
    platforms = ("azure",)
    cost_tier = "metered"
    default_name = "lab"
    params = [
        Param("os", "linux", "linux | windows-server"),
        Param("vm_size", None, "Student VM size from the allow-list. Default: Standard_B2s (linux) or Standard_D2s_v3 (windows-server)"),
        Param("nested", False, "Use a nested-virtualization size (for labs that run their own VMs)"),
        Param("students", 3, "Number of student VMs"),
        Param("max_students", safety.DEFAULT_MAX_STUDENTS, f"Hard cap on students (ceiling {safety.STUDENT_CEILING})"),
        Param("region", "canadacentral", "Azure region"),
        Param("expires_in_days", 14, "Tag the lab to expire after this many days (0 = never). `labagent sweep-azure` deletes expired labs"),
        Param("cohort", "", "Cohort tag (default: the lab name)"),
        Param("subscription", None, "Subscription ID or name (default: the one `az login` selected)"),
    ]
    output_keys = ("group", "region", "vm_size", "students")

    def services(self, profile):
        return {"az": _LazyAz()}

    # ---- helpers ------------------------------------------------------------------------------------------------------------
    def _az(self, ctx: PackContext):
        az = ctx.services["az"]
        if getattr(az, "subscription", None) is None and ctx.params.get("subscription"):
            az.subscription = ctx.params["subscription"]
        return az

    def _login(self, ctx: PackContext) -> str:
        az = self._az(ctx)
        sub = az.account()
        safety.check_subscription(sub["id"])
        return sub["id"]

    # ---- deploy -------------------------------------------------------------------------------------------------------------
    def _preflight(self, ctx: PackContext):
        p = safety.validate(ctx.params)
        sub = self._login(ctx)
        az = self._az(ctx)
        ctx.log("  checking VM sizes and quota in the region (the size list can take a minute) ...")
        family = az.sku_family(p["vm_size"], p["region"])
        gw_family = az.sku_family(safety.GATEWAY_SIZE, p["region"])
        total, families = lifecycle.peak_demand(family, p["vcpus"], p["students"], gw_family, safety.SIZES[safety.GATEWAY_SIZE])
        lifecycle.check_quota(az.usage(p["region"]), total, families, p["region"])
        group = safety.group_name(ctx.name)
        ctx.log(f"  subscription allowed | group: {group} | {p['students']} x {p['vm_size']} in {p['region']} | peak vCPUs: {total}")
        return {"subscription_id": sub, "group": group, "region": p["region"], "vm_size": p["vm_size"], "students": p["students"],
                "vcpu_peak": total}

    def _network_done(self, ctx: PackContext) -> bool:
        az = self._az(ctx)
        group = safety.group_name(ctx.name)
        if not az.group_exists(group):
            return False
        return any(r["type"].lower() == "microsoft.network/virtualnetworks" and safety.owned(r.get("tags"), ctx.name)
                   for r in az.resources(group))

    def _network(self, ctx: PackContext):
        az = self._az(ctx)
        group, region = ctx.state["group"], ctx.state["region"]
        existing = az.group_show(group)
        created = False
        if existing is None:
            tags = safety.lab_tags(ctx.name, ctx.params.get("cohort"), int(ctx.params["expires_in_days"]), created_group=True)
            az.group_create(group, region, tags)
            created = True
        elif not safety.owned(existing.get("tags"), ctx.name):
            raise PackError(f"resource group {group} already exists and is not this lab's: refusing to use it. Pick another --name.")
        tags = safety.lab_tags(ctx.name, ctx.params.get("cohort"), int(ctx.params["expires_in_days"]))
        if existing is not None:                  # keep the original created_at and expires_at on a re-run
            for k in ("created_at", "expires_at"):
                tags[k] = existing["tags"].get(k, tags[k])
        az.deploy(group, str(ARM_DIR / "network.json"), {"lab": safety.lab_name(ctx.name), "location": region, "tags": tags}, "labagent-network")
        ctx.log(f"  network ready in {group}")
        return {"network": True, "group_created": created or ctx.state.get("group_created", False)}

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        return [
            Step("preflight", "Check the Azure login, subscription allow list, size and vCPU quota", self._preflight),
            Step("network", "Create the lab resource group, virtual network and security groups", self._network, check=self._network_done),
        ]

    # ---- destroy ------------------------------------------------------------------------------------------------------------
    def _destroy(self, ctx: PackContext):
        self._login(ctx)
        group = safety.group_name(ctx.name)
        out = lifecycle.destroy_owned(self._az(ctx), group, ctx.name, ctx.log)
        ctx.log(f"  {'resource group deleted' if out['group_deleted'] else 'resource group kept'}; resources deleted: {out['resources_deleted']}")
        return {"group_deleted": out["group_deleted"], "network": False}

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("destroy", "Delete this lab's tagged resources, then the group if labagent created it", self._destroy)]
