"""gpu-notebooks: rent a GPU VM from a cloud provider, for JupyterLab labs on GPU slices (NVIDIA MIG).

This is part 1: the provider layer and the host. `launch` validates the offer (read only), creates one VM, and waits until it
is reachable. The VM deletes itself after `hours`, so a forgotten test cannot bill for days. `destroy` deletes only the VM this
launch created. Splitting the GPUs and starting the notebooks is the next part.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from ..base import Pack, PackContext, PackError, Param, Step, save_state
from . import providers
from .providers import GONE, Host, HostSpec

PACK_ID = "gpu-notebooks"
WAIT_S = 1200            # first boot of a GPU VM, including provider-side provisioning
POLL_S = 10


def host_name(name: str) -> str:
    return ("labagent-" + re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-"))[:50]


def host_tag(name: str) -> str:
    return f"labagent:{PACK_ID}:{name}"[:200]


class _LazyProviders:
    """Builds the real provider on first use, inside a step, so `--dry-run` needs no API key."""

    def __call__(self, provider_id: str):
        return providers.make(provider_id)


class GpuNotebooks(Pack):
    id = PACK_ID
    title = "GPU notebooks: JupyterLab labs on a rented GPU"
    summary = "Rent a GPU VM for JupyterLab labs"
    aliases = ("gpu-labs",)
    platforms = ("shadeform",)
    cost_tier = "metered"
    default_name = "gpu"
    params = [
        Param("cloud", "shadeform", f"Whose API to call: {', '.join(sorted(providers.PROVIDERS))}"),
        Param("instance_type", "H100", "The provider's GPU shape, e.g. H100 (one GPU) or H100x8"),
        Param("vendor", None, "shadeform: the datacenter company behind the offer, e.g. scaleway. Leave out to list what is available"),
        Param("region", None, "shadeform: that vendor's region, e.g. paris-france-1"),
        Param("os", None, "VM image from the provider's list, e.g. ubuntu22.04_cuda13.0_shade_os (default: the provider's)"),
        Param("ssh_key_id", None, "shadeform: id of an SSH key already added to your account (default: your account's default key)"),
        Param("hours", 4, "The provider deletes the VM after this many hours (1 to 72), whatever happens to this tool"),
    ]
    output_keys = ("cloud", "host_name", "host_id", "ip", "ssh_user", "ssh_port", "hourly_cents")

    def services(self, profile):
        return {"provider": _LazyProviders()}

    def _spec(self, ctx: PackContext) -> HostSpec:
        p = ctx.params
        try:
            hours = int(p["hours"])
        except (TypeError, ValueError):
            raise PackError("hours must be a whole number from 1 to 72")
        if not 1 <= hours <= 72:
            raise PackError("hours must be between 1 and 72")
        return HostSpec(name=host_name(ctx.name), instance_type=str(p["instance_type"]), vendor=p["vendor"], region=p["region"],
                        os_image=p["os"], ssh_key_id=p["ssh_key_id"], hours=hours, tags=["labagent", host_tag(ctx.name)])

    def _provider(self, ctx: PackContext):
        prov = ctx.services["provider"](ctx.params["cloud"])
        for w in getattr(prov, "warnings", []):
            ctx.log(f"  warning: {w}")
        return prov

    # ---- launch ----
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("preflight", "Check the offer (read only, nothing is created)", self._preflight),
                Step("host", "Create the GPU VM and wait until it is active", self._host, check=self._host_ready)]

    def _preflight(self, ctx: PackContext):
        spec = self._spec(ctx)
        info = self._provider(ctx).validate(spec)
        ctx.log(f"  {ctx.params['cloud']}: {spec.instance_type} from {spec.vendor} in {spec.region} at ${info['hourly_cents'] / 100:.2f}/h, "
                f"deleted by the provider after {spec.hours} h")
        return {"cloud": ctx.params["cloud"], "host_name": spec.name, "hourly_cents": info["hourly_cents"]}

    def _host_ready(self, ctx: PackContext) -> bool:
        hid = ctx.state.get("host_id")
        if not hid:
            return False
        h = self._provider(ctx).get(hid)
        return bool(h and h.status == "active" and h.ip)

    def _host(self, ctx: PackContext):
        prov = self._provider(ctx)
        spec = self._spec(ctx)
        h = prov.get(ctx.state["host_id"]) if ctx.state.get("host_id") else None
        if h is None or h.status in GONE:
            h = prov.find(spec.name, host_tag(ctx.name))        # a previous run may have created it and died before saving
            if h is None:
                hid = prov.create(spec)
                ctx.state["host_id"] = hid
                save_state(ctx.pack_id, ctx.name, ctx.state)     # now, not after the wait: a timeout must not lose the id of a billable VM
                h = Host(id=hid, name=spec.name)
                ctx.log(f"  created {hid}")
            else:
                ctx.log(f"  adopting the existing VM {h.id}")
        h = self._wait_active(ctx, prov, h.id)
        return {"host_id": h.id, "ip": h.ip, "ssh_user": h.ssh_user, "ssh_port": h.ssh_port}

    def _wait_active(self, ctx: PackContext, prov, host_id: str) -> Host:
        sleep = ctx.services.get("sleep", time.sleep)
        clock = ctx.services.get("clock", time.monotonic)
        deadline = clock() + WAIT_S
        while True:
            h = prov.get(host_id)
            if h is None or h.status in GONE:
                raise PackError("the VM was deleted while starting (the provider failed it, or `hours` ran out)")
            if any(w in h.status for w in ("error", "fail")):
                raise PackError(f"the provider reports the VM as '{h.status}'. Run destroy, then launch again")
            if h.status == "active" and h.ip:
                return h
            if clock() > deadline:
                raise PackError("the VM is not active yet after 20 minutes. Run launch again to keep waiting, or destroy to release it")
            sleep(POLL_S)

    # ---- destroy ----
    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("host", "Delete the GPU VM this launch created", self._delete)]

    def _delete(self, ctx: PackContext):
        prov = self._provider(ctx)
        hid = ctx.state.get("host_id")
        if not hid:                                              # launch died before saving: find it by exact name and our tag
            h = prov.find(host_name(ctx.name), host_tag(ctx.name))
            hid = h.id if h else None
        if not hid:
            ctx.log("  no VM to delete")
            return {}
        prov.delete(hid)
        ctx.log(f"  deleted {hid} (billing stops once the provider shows it as deleting)")
        return {"host_id": None}
