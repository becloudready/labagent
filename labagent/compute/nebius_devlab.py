"""Nebius Devlabs: Nebius's container product (Serverless AI). A Devlab is a container with a GPU, started from a curated template, and it
comes with its own HTTPS address. There is no VM, no ssh, no Docker and no MIG, so it is for ONE JupyterLab (gpu-notebooks-container), never for
the many-labs pack (gpu-notebooks), which needs a VM.

This provider shares the login, the project (so the region) and the platforms of NebiusProvider; only the product differs. Facts, each checked
on 2026-11-22 against Nebius's OpenAPI file (https://api.nebius.cloud/openapi.json) and with `dryRun` create calls on a real project (nothing created):

- REST: POST /ai/v1/devlabs (an asynchronous operation), GET /ai/v1/devlabs?parentId=, GET and DELETE /ai/v1/devlabs/{id}.
- The Jupyter template is `vmapptemplate-g00devlab-jupyter` (PyTorch JupyterLab, persistent /home). It needs `jupyter_token`: exactly 16 letters and digits.
  That token is the JupyterLab login, so labagent chooses it (HostSpec.jupyter_password) and prints it.
- The GPU is `gpu: ["H100:1"]` (models: b200, b300, h100, h200, l40s, rtx6000). The container disk must be at least 90 GiB for this template.
- status.state is PROVISIONING, STARTING, IMAGE_PULLING, RUNNING, STOPPING, STOPPED, DELETING, FAILED or ERROR. status.publicEndpoints is filled only
  while the Devlab is RUNNING and its tunnel is ready: that is the URL.
- There is no automatic deletion and no price in the API: a Devlab bills until `destroy`. Prices are the public GPU-hour list prices used for VMs
  (the container disk is billed on top and is not in these figures). Deleting a Devlab deletes its disk.

- sshAuthorizedKeys (public keys) and publicIp (bool) are in DevlabSpec; status.instances[].publicIp is the address. labagent sends the shared key with
  publicIp true when it has one. The accepted request was checked with dryRun; that ssh connects, and as which user, is NOT verified.

NOT verified on a real Devlab yet: that a created Devlab reaches RUNNING, that its URL opens without a Nebius sign-in (Nebius says its tunnel
authorizes access with IAM), and that the token is accepted by JupyterLab.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from .errors import ComputeError
from .nebius import VRAM_GB, NebiusProvider, _label
from .providers import NAME_RE, ApiError, Host, HostSpec, Offer, OfferQuery

VENDOR_DEVLAB = "nebius-devlab"
TEMPLATE_JUPYTER = "vmapptemplate-g00devlab-jupyter"
DISK_GIB = 100                                         # Nebius's minimum for this template is 90 GiB
PATH = "/ai/v1/devlabs"


class NebiusDevlabProvider(NebiusProvider):
    """Nebius Devlabs as a cloud of containers with the cloud's own JupyterLab."""

    id = "nebius-devlab"
    title = "Nebius Devlabs"
    capabilities = ("offers", "launch")
    vendor = VENDOR_DEVLAB
    offer_kind = "container"
    offer_os: tuple = ()
    container_only = True
    builtin_jupyter = True
    has_ssh = False                                    # no ssh by default; the shared key is installed when there is one (ssh_optional)
    ssh_optional = True

    # ---- offers: the same platforms as the VMs, as containers ----
    def offers(self, query: OfferQuery) -> List[Offer]:
        out = super().offers(query)
        for o in out:
            o.storage_gb = DISK_GIB
            if not o.vram_gb:
                o.vram_gb = VRAM_GB.get(o.gpu_type, 0)
        return out

    # ---- plumbing ----
    @staticmethod
    def _host(d: Dict[str, Any]) -> Host:
        meta, status = d.get("metadata") or {}, d.get("status") or {}
        state = str(status.get("state") or "UNSPECIFIED").upper()
        urls = [str(u) for u in status.get("publicEndpoints") or [] if u]
        mapped = {"RUNNING": "active" if urls else "pending", "STOPPING": "stopped", "STOPPED": "stopped", "DELETING": "deleting",
                  "ERROR": "error", "FAILED": "failed"}.get(state, "pending")
        ips = [str(i.get("publicIp")) for i in status.get("instances") or [] if isinstance(i, dict) and i.get("publicIp")]
        return Host(id=str(meta.get("id") or ""), name=str(meta.get("name") or ""), status=mapped, ip=ips[0] if ips else None, ssh_user=None, ssh_port=None,
                    tags=[str(v) for v in (meta.get("labels") or {}).values()], url=urls[0].rstrip("/") if urls else None)

    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        if not NAME_RE.fullmatch(spec.name):
            raise ComputeError("the host name must be 1 to 50 letters, digits, - or _")
        region = self._region()
        if spec.region and spec.region != region and spec.region.lower() != region.lower():
            raise ComputeError(f"the Nebius project {self._project()} is in {region}, not {spec.region}. Use a project in that region (NEBIUS_PROJECT_ID)")
        if spec.os_image:
            raise ComputeError("a Nebius Devlab starts Nebius's own JupyterLab template: it takes no image. Leave `os` out")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        if not offers:
            menu = "; ".join(f"{o.instance_type} ${o.hourly_usd:.2f}/h" for o in self.offers(OfferQuery())[:6]) or "none"
            raise ComputeError(f"{spec.instance_type} is not offered in the Nebius project's region {region}. Offered there: {menu}. "
                               f"See all: labagent compute offers --cloud {self.id}")
        best = offers[0]
        return {"hourly_cents": round(best.hourly_usd * 100), "os_options": [], "gpu_type": best.gpu_type, "instance_type": best.instance_type}

    def _body(self, spec: HostSpec, offer: Offer) -> Dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9]{16}", spec.jupyter_password or ""):
            raise ComputeError("a Nebius Devlab needs a JupyterLab token (HostSpec.jupyter_password) of exactly 16 letters and digits")
        body = {"metadata": {"parentId": self._project(), "name": spec.name, "labels": {f"labagent-{i}": _label(t) for i, t in enumerate(spec.tags)}},
                "spec": {"template": {"id": TEMPLATE_JUPYTER, "inputFieldValues": {"jupyter_token": spec.jupyter_password}},
                         "gpu": [f"{offer.gpu_type}:{offer.num_gpus}"],
                         "disk": {"sizeBytes": str(DISK_GIB * 1024 ** 3), "type": "NETWORK_SSD"}, "workspace": {}}}
        if spec.ssh_public_key:                           # the shared labagent key: optional, a Devlab works without it (JupyterLab has a terminal)
            body["spec"]["sshAuthorizedKeys"] = [" ".join(str(spec.ssh_public_key).split()[:3])]
            body["spec"]["publicIp"] = True
        return body

    # ---- lifecycle ----
    def find(self, name: str, tag: str) -> Optional[Host]:
        try:
            items = self._list(PATH)
        except ApiError as e:
            raise self._fail(e, "list Devlabs")
        for d in items:
            h = self._host(d)
            if h.name == name and _label(tag) in h.tags and h.status not in ("deleting", "deleted"):
                return h
        return None

    def create(self, spec: HostSpec) -> str:
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        if not offers:
            raise ComputeError(f"{spec.instance_type} is not offered in the Nebius project's region")
        try:
            op = self._call("POST", PATH, self._body(spec, offers[0])) or {}
        except ApiError as e:
            raise self._fail(e, "create the Devlab")
        host_id = str(op.get("resourceId") or "")
        failure = self._devlab_failure(str(op.get("id") or ""), op)
        if failure:
            raise ComputeError(f"Nebius could not create the Devlab: {failure}")
        if not host_id:
            found = self.find(spec.name, spec.tags[-1] if spec.tags else "")
            if not found:
                raise ComputeError("Nebius accepted the request but returned no Devlab id: check Serverless AI > Devlabs in the Nebius console for " + spec.name)
            host_id = found.id
        return host_id

    def _devlab_failure(self, op_id: str, op: Dict[str, Any]) -> str:
        """Same wait as for a VM, on the Devlab's operation endpoint."""
        waited = 0
        while True:
            status = op.get("status") or {}
            if op.get("finishedAt") or status:
                return "" if not status.get("code") else str(status.get("message") or "failed")[:300]
            if not op_id or waited >= 8:
                return ""
            self._sleep(2)
            waited += 2
            try:
                op = self._call("GET", f"{PATH}/operations/{urllib.parse.quote(op_id, safe='')}") or {}
            except ApiError:
                return ""

    def get(self, host_id: str) -> Optional[Host]:
        try:
            d = self._call("GET", f"{PATH}/{urllib.parse.quote(host_id, safe='')}")
        except ApiError as e:
            if self._gone(e):
                return None
            raise self._fail(e, "read the Devlab")
        return self._host(d) if d and (d.get("metadata") or {}).get("id") else None

    def delete(self, host_id: str) -> None:
        try:
            self._call("DELETE", f"{PATH}/{urllib.parse.quote(host_id, safe='')}")
        except ApiError as e:
            if self._gone(e):
                return
            raise self._fail(e, "delete the Devlab")

    def web_url(self, host_id: str, port: int) -> Optional[str]:
        h = self.get(host_id)
        return h.url if h else None                       # the one address Nebius gives the Devlab's primary web port (JupyterLab)
