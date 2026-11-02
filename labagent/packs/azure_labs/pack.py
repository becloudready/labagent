"""azure-labs: lift and shift Azure VMs from one resource group to another, or just snapshot them.

An alternative for Azure Lab Services customers who need their lab VMs somewhere else. Not Microsoft's service.
`launch` copies a source group's VMs, disks and network into a target group and never changes the source.
`destroy` deletes only what that launch created.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..base import Pack, PackContext, PackError, Param, Step
from . import lifecycle, migrate, safety
from .azcli import AzCli


class _LazyAz:
    """Creates the real `az` wrapper on first use, so `--dry-run` works without Azure installed."""

    def __init__(self) -> None:
        self._az = None
        self.subscription = None

    def __getattr__(self, name: str):
        if self._az is None:
            self._az = AzCli(self.subscription)
        return getattr(self._az, name)


def _names(text: Any) -> List[str]:
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


class AzureLabs(Pack):
    id = safety.PACK_ID
    title = "Azure Labs: move Azure VMs between resource groups"
    summary = "Lift and shift Azure VMs, disks and network to another resource group"
    aliases = ("azure-lab", "als-migration")
    platforms = ("azure",)
    cost_tier = "metered"
    default_name = "migration"
    params = [
        Param("source_rg", None, "Resource group to copy from. It is only read, never changed"),
        Param("target_rg", None, "Resource group to copy into. Created if it does not exist"),
        Param("vms", "", "Comma list of VM names to copy (default: every VM in the source group)"),
        Param("snapshot_only", False, "Only snapshot the disks into the target group: no VMs, no network. Also takes unattached disks when vms is empty"),
        Param("subscription", None, "Subscription ID or name (default: the one `az login` selected)"),
    ]
    output_keys = ("source_rg", "target_rg", "vms", "snapshots")

    def services(self, profile):
        return {"az": _LazyAz()}

    def _az(self, ctx: PackContext):
        az = ctx.services["az"]
        if getattr(az, "subscription", None) is None and ctx.params.get("subscription"):
            az.subscription = ctx.params["subscription"]
        return az

    def _login(self, ctx: PackContext) -> str:
        sub = self._az(ctx).account()["id"]
        safety.check_subscription(sub)
        return sub

    def _tags(self, ctx: PackContext, created_group: bool = False) -> Dict[str, str]:
        return safety.tags(ctx.name, ctx.state["source_rg"], created_group)

    # ---- launch -------------------------------------------------------------------------------------------------------------
    def _preflight(self, ctx: PackContext):
        p = ctx.params
        source, target = p["source_rg"], p["target_rg"]
        if not source or not target:
            raise PackError("give --param source_rg=<group to copy from> and --param target_rg=<group to copy into>")
        if source.lower() == target.lower():
            raise PackError("source_rg and target_rg must be different")
        safety.lab_name(ctx.name)
        self._login(ctx)
        az = self._az(ctx)
        if not az.group_exists(source):
            raise PackError(f"source resource group {source} does not exist")
        inv = migrate.inspect(az, source, _names(p["vms"]), bool(p["snapshot_only"]))
        ctx.mem["inv"] = inv
        n_disks = sum(len(v["disks"]) for v in inv["vms"]) + len(inv["disks"])
        if not p["snapshot_only"] and inv["vms"]:
            regions = {v["location"] for v in inv["vms"]}
            if len(regions) > 1:
                raise PackError(f"the VMs are in more than one region ({', '.join(sorted(regions))}): copy them in separate runs with vms=")
            region = regions.pop()
            ctx.log("  checking VM sizes and quota (the size list can take a minute) ...")
            skus = az.skus(region)
            needs: Dict[str, int] = {}
            for v in inv["vms"]:
                s = skus.get(v["size"])
                if s is None or s.get("restricted"):
                    raise PackError(f"{v['size']} ({v['name']}) is not available to this subscription in {region}")
                needs[s["family"]] = needs.get(s["family"], 0) + int(s["vcpus"])
            lifecycle.check_quota(az.usage(region), needs, region)
        ctx.log(f"  {source} -> {target}: {len(inv['vms'])} VM(s), {n_disks} disk(s)" + (", snapshots only" if p["snapshot_only"] else ""))
        return {"source_rg": source, "target_rg": target, "vms": [v["name"] for v in inv["vms"]], "snapshot_only": bool(p["snapshot_only"])}

    def _inv(self, ctx: PackContext) -> Dict[str, Any]:
        return ctx.mem["inv"]

    def _target(self, ctx: PackContext):
        az = self._az(ctx)
        target = ctx.state["target_rg"]
        if not az.group_exists(target):
            inv = self._inv(ctx)
            region = (inv["vms"] or inv["disks"])[0]["location"]
            az.group_create(target, region, self._tags(ctx, created_group=True))
            ctx.log(f"  created resource group {target}")
        return {}

    def _snapshot(self, ctx: PackContext):
        n = migrate.snapshots(self._az(ctx), self._inv(ctx), ctx.state["target_rg"], self._tags(ctx), ctx.log)
        return {"snapshots": sum(len(v["disks"]) for v in self._inv(ctx)["vms"]) + len(self._inv(ctx)["disks"]), "snapshots_made": n}

    def _network(self, ctx: PackContext):
        migrate.network(self._az(ctx), self._inv(ctx), ctx.state["target_rg"], self._tags(ctx), ctx.log)
        return {}

    def _disks(self, ctx: PackContext):
        migrate.disks(self._az(ctx), self._inv(ctx), ctx.state["target_rg"], self._tags(ctx), ctx.log)
        return {}

    def _vms(self, ctx: PackContext):
        migrate.vms(self._az(ctx), self._inv(ctx), ctx.state["target_rg"], self._tags(ctx), ctx.log)
        return {}

    def _verify(self, ctx: PackContext):
        az, target = self._az(ctx), ctx.state["target_rg"]
        found = {(r["type"].lower(), r["name"]) for r in az.resources(target)}
        missing = [f"snapshot {migrate.snapshot_name(d['name'])}" for v in self._inv(ctx)["vms"] for d in v["disks"]
                   if (migrate.SNAPSHOT, migrate.snapshot_name(d["name"])) not in found]
        missing += [f"snapshot {migrate.snapshot_name(d['name'])}" for d in self._inv(ctx)["disks"] if (migrate.SNAPSHOT, migrate.snapshot_name(d["name"])) not in found]
        report = {}
        if not ctx.state.get("snapshot_only"):
            for v in self._inv(ctx)["vms"]:
                info = az.vm_state(target, v["name"])
                ok = info.get("provisioningState") == "Succeeded"
                report[v["name"]] = {"state": info.get("powerState"), "private_ip": info.get("privateIps"), "public_ip": info.get("publicIps") or None}
                ctx.log(f"  {v['name']}: {info.get('provisioningState')}, {info.get('powerState')}, {info.get('privateIps')}")
                if not ok:
                    missing.append(f"VM {v['name']} ({info.get('provisioningState')})")
        if missing:
            raise PackError(f"verify failed: {', '.join(missing)}")
        return {"report": report}

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        only = bool(ctx.params.get("snapshot_only"))
        steps = [Step("preflight", "Read the source group, check the login, allow list and quota", self._preflight),
                 Step("target", "Create the target resource group if it is missing", self._target),
                 Step("snapshot", "Snapshot every disk into the target group", self._snapshot)]
        if not only:
            steps += [Step("network", "Copy the virtual network, security groups, public IPs and NICs", self._network),
                      Step("disks", "Create disks from the snapshots", self._disks),
                      Step("vms", "Create the VMs from the copied disks", self._vms)]
        return steps + [Step("verify", "Check that everything exists and the VMs are provisioned", self._verify)]

    # ---- destroy ------------------------------------------------------------------------------------------------------------
    def _destroy(self, ctx: PackContext):
        target = ctx.params.get("target_rg") or ctx.state.get("target_rg")
        if not target:
            raise PackError("give --param target_rg=<group the migration copied into>")
        if ctx.state.get("source_rg", "").lower() == target.lower():
            raise PackError("refusing to delete from the source group")
        self._login(ctx)
        out = lifecycle.destroy_owned(self._az(ctx), target, ctx.name, ctx.log)
        ctx.log(f"  resources deleted: {out['resources_deleted']}; resource group {'deleted' if out['group_deleted'] else 'kept'}")
        return {"group_deleted": out["group_deleted"]}

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("destroy", "Delete what this migration created in the target group (the source is never touched)", self._destroy)]
