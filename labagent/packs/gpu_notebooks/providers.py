"""GPU cloud providers for gpu-notebooks. A provider rents and releases one GPU host; the pack does the rest.

To add a cloud: subclass `Provider`, implement its methods, and register it in `PROVIDERS`. The pack never imports a
provider directly, so nothing else changes. API keys are read from the environment or from ~/.labagent/credentials, are only
ever sent to their own provider, and never appear in state, the envelope, logs or error messages.
"""
from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..base import PackError

USER_AGENT = "labagent-gpu-notebooks (+https://github.com/becloudready/labagent)"


@dataclass
class HostSpec:
    name: str                       # provider-side name (letters, digits, - and _)
    instance_type: str              # provider's GPU shape, e.g. H100 or H100x8
    vendor: Optional[str] = None    # the datacenter company behind the offer (Shadeform calls this field `cloud`)
    region: Optional[str] = None
    os_image: Optional[str] = None
    ssh_key_id: Optional[str] = None
    hours: int = 4                  # the provider deletes the host after this long, so a forgotten lab cannot bill for days
    shade_cloud: bool = True
    tags: List[str] = field(default_factory=list)


@dataclass
class Host:
    id: str
    name: str = ""
    status: str = "pending"         # pending | active | deleting | deleted, or a provider error state
    ip: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_port: Optional[int] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class OfferQuery:
    """What to look for. Every field is optional; text matches ignore case."""
    gpu_type: Optional[str] = None          # the GPU family, e.g. H100
    num_gpus: Optional[int] = None
    instance_type: Optional[str] = None     # the cloud's own shape name, e.g. H100x8
    vendor: Optional[str] = None
    region: Optional[str] = None
    kind: Optional[str] = None              # vm | baremetal | container
    max_price: Optional[float] = None       # dollars per hour
    available_only: bool = True


@dataclass
class Offer:
    """One shape in one region: what `launch` needs (vendor, region, instance_type, an os) and what a person picks by."""
    instance_type: str
    vendor: str
    region: str
    hourly_usd: float
    available: bool = True
    region_name: str = ""
    gpu_type: str = ""
    num_gpus: int = 0
    vram_gb: int = 0                        # per GPU
    vcpus: int = 0
    memory_gb: int = 0
    storage_gb: int = 0
    interconnect: str = ""
    kind: str = ""
    boot_seconds: int = 0                   # the slowest the cloud says it can take
    os_options: List[str] = field(default_factory=list)


@dataclass
class SshKey:
    id: str
    name: str
    is_default: bool = False


class Provider:
    id = ""
    warnings: List[str]

    def offers(self, query: OfferQuery) -> List[Offer]:
        """Read only. Every shape that matches, cheapest first, one entry per region."""
        raise NotImplementedError

    def ssh_keys(self) -> List[SshKey]:
        """Read only. The SSH keys already added to the account. Never returns key material."""
        raise NotImplementedError

    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        """Read only. Raise PackError if this host cannot be created as asked. Return non-secret facts (price)."""
        raise NotImplementedError

    def find(self, name: str, tag: str) -> Optional[Host]:
        """A live host with exactly this name AND tag, to adopt after a crash between create and save. Never fuzzy."""
        raise NotImplementedError

    def create(self, spec: HostSpec) -> str:
        raise NotImplementedError

    def get(self, host_id: str) -> Optional[Host]:
        """None when the provider no longer knows the host."""
        raise NotImplementedError

    def delete(self, host_id: str) -> None:
        raise NotImplementedError


# ---- credentials ------------------------------------------------------------------------------------------------------

def credentials_path() -> Path:
    return Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "credentials"


def read_secret(section: str, env_var: str, path: Optional[Path] = None) -> Tuple[Optional[str], List[str]]:
    """The environment variable wins. Otherwise the [section] of the credentials file: either `key = value` or a bare value line.
    Returns (secret or None, warnings). A file other users can read gets a warning, never an error."""
    if os.environ.get(env_var):
        return os.environ[env_var].strip(), []
    p = path or credentials_path()
    if not p.is_file():
        return None, []
    warnings: List[str] = []
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.append(f"{p} is readable by other users: run chmod 600 on it")
    current = None
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[([^\]]+)\]", line)
        if m:
            current = m.group(1).strip().lower()
            continue
        if current != section.lower():
            continue
        kv = re.fullmatch(r"([A-Za-z_][\w.-]*)\s*[=:]\s*(.+)", line)
        value = kv.group(2) if kv else line
        value = value.strip().strip("'\"")
        if value:
            return value, warnings
    return None, warnings


# ---- Shadeform --------------------------------------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


Http = Callable[[str, str, Optional[dict]], Any]
BASE = "https://api.shadeform.ai"
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,50}")
GONE = ("deleting", "deleted")


def make_http(api_key: str) -> Http:
    def call(method: str, path: str, body: Optional[dict] = None) -> Any:
        req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-API-KEY": api_key, "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https endpoint)
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")[:300])
        except (urllib.error.URLError, OSError):
            raise PackError("cannot reach api.shadeform.ai (network error)")
        return json.loads(raw) if raw.strip() else {}
    return call


class ShadeformProvider(Provider):
    """Shadeform rents GPU VMs from many clouds behind one API. https://docs.shadeform.ai/api-reference"""

    id = "shadeform"

    def __init__(self, api_key: Optional[str] = None, http: Optional[Http] = None):
        self.warnings: List[str] = []
        if http is None:
            key = api_key
            if not key:
                key, self.warnings = read_secret("shadeform", "SHADEFORM_API_KEY")
            if not key:
                raise PackError(f"no Shadeform API key: set SHADEFORM_API_KEY or put it under [shadeform] in {credentials_path()}")
            http = make_http(key)
        self._http = http

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        try:
            return self._http(method, path, body)
        except ApiError as e:
            if e.status in (401, 403):
                raise PackError("Shadeform rejected the API key: check SHADEFORM_API_KEY or ~/.labagent/credentials")
            raise

    @staticmethod
    def _fail(e: ApiError, what: str) -> PackError:
        detail = e.body
        try:
            j = json.loads(e.body)
            detail = j.get("error") or detail
        except ValueError:
            pass
        return PackError(f"Shadeform could not {what} ({e}): {str(detail)[:200]}")

    @staticmethod
    def _host(d: Dict[str, Any]) -> Host:
        return Host(id=d["id"], name=d.get("name") or "", status=str(d.get("status") or "pending").lower(), ip=d.get("ip") or None,
                    ssh_user=d.get("ssh_user"), ssh_port=d.get("ssh_port"), tags=list(d.get("tags") or []))

    def offers(self, query: OfferQuery) -> List[Offer]:
        # The server narrows the answer. Region and the rest are checked here as well, because a filter the server ignores
        # must never widen what we report.
        want = {"sort": "price"}
        if query.available_only:
            want["available"] = "true"
        for key, value in (("gpu_type", query.gpu_type), ("num_gpus", query.num_gpus), ("shade_instance_type", query.instance_type),
                           ("cloud", query.vendor)):
            if value not in (None, ""):
                want[key] = str(value)
        try:
            data = self._call("GET", "/v1/instances/types?" + urllib.parse.urlencode(want))
        except ApiError as e:
            raise self._fail(e, "list instance types")

        def same(a: Any, b: Optional[str]) -> bool:
            return b is None or str(a or "").lower() == b.lower()
        out: List[Offer] = []
        for t in data.get("instance_types", []):
            conf = t.get("configuration") or {}
            price = int(t.get("hourly_price") or 0) / 100
            gpu = t.get("gpu_type") or conf.get("gpu_type") or ""
            count = int(t.get("num_gpus") or conf.get("num_gpus") or 0)
            if not (same(gpu, query.gpu_type) and same(t.get("shade_instance_type"), query.instance_type) and same(t.get("cloud"), query.vendor)
                    and same(t.get("deployment_type"), query.kind) and (query.num_gpus is None or count == query.num_gpus)
                    and (query.max_price is None or price <= query.max_price)):
                continue
            for a in t.get("availability") or []:
                if a.get("rental_type", "on_demand") != "on_demand":
                    continue                        # spot capacity can be taken back at any moment: not for a lab
                if query.available_only and not a.get("available"):
                    continue
                if not same(a.get("region"), query.region):
                    continue
                out.append(Offer(
                    instance_type=str(t.get("shade_instance_type") or ""), vendor=str(t.get("cloud") or ""), region=str(a.get("region") or ""),
                    hourly_usd=round(price, 2), available=bool(a.get("available")), region_name=str(a.get("display_name") or ""),
                    gpu_type=str(gpu), num_gpus=count, vram_gb=int(conf.get("vram_per_gpu_in_gb") or 0),
                    vcpus=int(t.get("vcpus") or conf.get("vcpus") or 0), memory_gb=int(t.get("memory_in_gb") or conf.get("memory_in_gb") or 0),
                    storage_gb=int(t.get("storage_in_gb") or conf.get("storage_in_gb") or 0),
                    interconnect=str(t.get("interconnect") or conf.get("interconnect") or ""), kind=str(t.get("deployment_type") or ""),
                    boot_seconds=int((t.get("boot_time") or {}).get("max_boot_in_sec") or 0), os_options=list(conf.get("os_options") or [])))
        out.sort(key=lambda o: (o.hourly_usd, o.vendor, o.region, o.instance_type))
        return out

    def ssh_keys(self) -> List[SshKey]:
        try:
            data = self._call("GET", "/v1/sshkeys")
        except ApiError as e:
            raise self._fail(e, "list SSH keys")
        return [SshKey(id=str(k["id"]), name=str(k.get("name") or ""), is_default=bool(k.get("is_default")))
                for k in data.get("ssh_keys", []) if k.get("id")]

    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        if not NAME_RE.fullmatch(spec.name):
            raise PackError("the host name must be 1 to 50 letters, digits, - or _")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        menu = "; ".join(f"vendor={o.vendor} region={o.region} ${o.hourly_usd:.2f}/h" for o in offers[:5]) or "none available right now"

        def refuse(why: str) -> PackError:
            return PackError(f"{why}. Available {spec.instance_type}: {menu}. See all: labagent offers --type {spec.instance_type}")
        if not spec.vendor or not spec.region:
            raise refuse("set vendor and region")
        match = [o for o in offers if o.vendor.lower() == spec.vendor.lower() and o.region == spec.region]
        if not match:
            raise refuse(f"{spec.instance_type} is not available on {spec.vendor} in {spec.region}")
        if spec.os_image and spec.os_image not in match[0].os_options:
            raise PackError(f"image '{spec.os_image}' is not offered there. Offered: {', '.join(match[0].os_options) or 'none listed'}")
        return {"hourly_cents": round(match[0].hourly_usd * 100), "os_options": match[0].os_options, "gpu_type": match[0].gpu_type}

    def find(self, name: str, tag: str) -> Optional[Host]:
        try:
            data = self._call("GET", "/v1/instances")
        except ApiError as e:
            raise self._fail(e, "list instances")
        for d in data.get("instances", []):
            h = self._host(d)
            if h.name == name and tag in h.tags and h.status not in GONE:
                return h
        return None

    def create(self, spec: HostSpec) -> str:
        due = (datetime.now(timezone.utc) + timedelta(hours=spec.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body: Dict[str, Any] = {"cloud": spec.vendor, "name": spec.name, "region": spec.region, "shade_instance_type": spec.instance_type,
                                "shade_cloud": spec.shade_cloud, "tags": spec.tags, "auto_delete": {"date_threshold": due}}
        if spec.os_image:
            body["os"] = spec.os_image
        if spec.ssh_key_id:
            body["ssh_key_id"] = spec.ssh_key_id
        try:
            return self._call("POST", "/v1/instances/create", body)["id"]
        except ApiError as e:
            raise self._fail(e, "create the instance")

    def get(self, host_id: str) -> Optional[Host]:
        try:
            return self._host(self._call("GET", f"/v1/instances/{host_id}/info"))
        except ApiError as e:
            if e.status == 404:
                return None
            raise self._fail(e, "read the instance")

    def delete(self, host_id: str) -> None:
        try:
            self._call("POST", f"/v1/instances/{host_id}/delete")
        except ApiError as e:
            if e.status == 404:
                return                      # already gone
            raise self._fail(e, "delete the instance")


PROVIDERS: Dict[str, Callable[[], Provider]] = {"shadeform": ShadeformProvider}


def make(provider_id: str) -> Provider:
    if provider_id not in PROVIDERS:
        raise PackError(f"unknown cloud '{provider_id}'. Available: {', '.join(sorted(PROVIDERS))}")
    return PROVIDERS[provider_id]()
