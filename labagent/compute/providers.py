"""Compute clouds. A provider lists what it rents (offers), the SSH keys in the account, and creates and releases one host.

To add a cloud: subclass `Provider`, implement its methods, set `id`, `title`, `capabilities` and `env_var`, and register it in
`PROVIDERS`. Nothing that uses a provider imports a specific one. API keys are read from the environment or from
~/.labagent/credentials, are only ever sent to their own provider, and never appear in state, the envelope, logs or error messages.
"""
from __future__ import annotations

import base64
import hashlib
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

from .errors import ComputeError

USER_AGENT = "labagent-compute (+https://github.com/becloudready/labagent)"


# Where a machine is, in the words a learner uses. The country is the two-letter code a cloud puts first in a region's display name
# ("US, Dallas, TX"). A region with no country code has no area: it is only found by "any".
AREAS: Dict[str, Tuple[str, frozenset]] = {
    "europe": ("Europe", frozenset("AT BE BG CH CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LT LU LV NL NO PL PT RO SE SI SK UA".split())),
    "north-america": ("North America", frozenset("US CA MX".split())),
    "asia-pacific": ("Asia Pacific", frozenset("JP IN SG AU NZ KR HK TW ID MY TH VN PH".split())),
}
LOCATIONS = ("any",) + tuple(AREAS)


def area_of(region_name: str) -> str:
    """europe, north-america or asia-pacific, or "" when the region does not say or is somewhere else."""
    m = re.match(r"\s*([A-Z]{2})\s*,", region_name or "")
    return next((key for key, (_, codes) in AREAS.items() if m and m.group(1) in codes), "")


def best_os(options: List[str]) -> str:
    """The newest CUDA image, then the newest Ubuntu, of the form ubuntu24.04_cuda13.0_shade_os. "" when none looks like one."""
    def rank(o: str):
        m = re.fullmatch(r"ubuntu(\d+)\.(\d+)_cuda(\d+)\.(\d+)_shade_os", o)
        return (int(m.group(3)), int(m.group(4)), int(m.group(1)), int(m.group(2))) if m else None
    ranked = [(rank(o), o) for o in options if rank(o)]
    return max(ranked)[1] if ranked else ""


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
    area: Optional[str] = None              # europe | north-america | asia-pacific ("any" or None: everywhere)


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
    area_hint: str = ""                     # set by a cloud whose region names carry no country code (Lambda); wins over the name

    @property
    def area(self) -> str:
        return self.area_hint or area_of(self.region_name)


@dataclass
class SshKey:
    id: str
    name: str
    is_default: bool = False
    fingerprint: str = ""                   # SHA256:... of the PUBLIC half, so a person can match it to a file. Never the key itself


def fingerprint_of(public_key: str) -> str:
    """`SHA256:<base64>` of one OpenSSH public key line (what `ssh-keygen -lf` prints), or '' if it is not one."""
    parts = str(public_key or "").split()
    if len(parts) < 2:
        return ""
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError):
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=") if blob else ""


class Provider:
    id = ""                                 # what --cloud takes
    title = ""                              # what a person calls it
    capabilities: Tuple[str, ...] = ()      # any of: offers, ssh_keys, launch
    env_var = ""                            # where its API key may be set; the same key may also live in the credentials file
    key_file = ""                           # optional: a file under ~/.labagent/keys/ that holds just the API key
    auto_delete = True                      # does the cloud delete a host by itself after `hours`? False: only labagent can, so say so
    warnings: List[str]

    def offers(self, query: OfferQuery) -> List[Offer]:
        """Read only. Every shape that matches, cheapest first, one entry per region."""
        raise NotImplementedError

    def ssh_keys(self) -> List[SshKey]:
        """Read only. The SSH keys already added to the account. Never returns key material."""
        raise NotImplementedError

    def add_ssh_key(self, name: str, public_key: str) -> Tuple[SshKey, bool]:
        """Register a PUBLIC key with the account. Returns (key, newly_added): a key already there is reused, never added twice."""
        raise NotImplementedError

    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        """Read only. Raise ComputeError if this host cannot be created as asked. Return non-secret facts (price)."""
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


def keys_dir() -> Path:
    return Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "keys"


def _loose(p: Path) -> bool:
    return bool(p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def find_secret(section: str, env_var: str, path: Optional[Path] = None, key_file: str = "") -> Tuple[Optional[str], Optional[str], List[str]]:
    """(secret or None, where it came from, warnings). The environment variable wins. Then the [section] of the credentials file:
    either `key = value` or a bare value line. Then, if the cloud has one, the file ~/.labagent/keys/<key_file> holding just the key.
    A file other users can read gets a warning, never an error."""
    if os.environ.get(env_var):
        return os.environ[env_var].strip(), "environment", []
    warnings: List[str] = []
    p = path or credentials_path()
    if p.is_file():
        if _loose(p):
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
                return value, "credentials file", warnings
    kf = keys_dir() / key_file if key_file else None
    if kf is not None and kf.is_file():
        value = kf.read_text(encoding="utf-8").strip().strip("'\"")
        if value and "\n" not in value:
            return value, "key file", warnings + ([f"{kf} is readable by other users: run chmod 600 on it"] if _loose(kf) else [])
    return None, None, warnings


def read_secret(section: str, env_var: str, path: Optional[Path] = None, key_file: str = "") -> Tuple[Optional[str], List[str]]:
    """(secret or None, warnings): see find_secret."""
    value, _, warnings = find_secret(section, env_var, path, key_file)
    return value, warnings


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
            raise ComputeError("cannot reach api.shadeform.ai (network error)")
        return json.loads(raw) if raw.strip() else {}
    return call


class ShadeformProvider(Provider):
    """Shadeform rents GPU VMs from many clouds behind one API. https://docs.shadeform.ai/api-reference"""

    id = "shadeform"
    title = "Shadeform"
    capabilities = ("offers", "ssh_keys", "launch")
    env_var = "SHADEFORM_API_KEY"

    def __init__(self, api_key: Optional[str] = None, http: Optional[Http] = None):
        self.warnings: List[str] = []
        if http is None:
            key = api_key
            if not key:
                key, self.warnings = read_secret("shadeform", "SHADEFORM_API_KEY")
            if not key:
                raise ComputeError(f"no Shadeform API key: set SHADEFORM_API_KEY or put it under [shadeform] in {credentials_path()}")
            http = make_http(key)
        self._http = http

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        try:
            return self._http(method, path, body)
        except ApiError as e:
            if e.status in (401, 403):
                raise ComputeError("Shadeform rejected the API key: check SHADEFORM_API_KEY or ~/.labagent/credentials")
            raise

    @staticmethod
    def _fail(e: ApiError, what: str) -> ComputeError:
        detail = e.body
        try:
            j = json.loads(e.body)
            detail = j.get("error") or detail
        except ValueError:
            pass
        return ComputeError(f"Shadeform could not {what} ({e}): {str(detail)[:200]}")

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
                if query.area not in (None, "", "any") and area_of(str(a.get("display_name") or "")) != query.area:
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
        # Only the public half is read, and only to compute its fingerprint. A `private_key` field (Shadeform keeps the private half
        # of keys it generated) is never looked at.
        return [SshKey(id=str(k["id"]), name=str(k.get("name") or ""), is_default=bool(k.get("is_default")),
                       fingerprint=fingerprint_of(k.get("public_key") or ""))
                for k in data.get("ssh_keys", []) if k.get("id")]

    def add_ssh_key(self, name: str, public_key: str) -> Tuple[SshKey, bool]:
        want = fingerprint_of(public_key)
        if not want:
            raise ComputeError("that is not an OpenSSH public key line (expected: ssh-ed25519 AAAA... comment)")
        for k in self.ssh_keys():
            if k.fingerprint == want:
                return k, False
        line = " ".join(public_key.split()[:3])                 # type, key and comment: no newline, nothing else
        try:
            data = self._call("POST", "/v1/sshkeys/add", {"name": name, "public_key": line})
        except ApiError as e:
            raise self._fail(e, "add the SSH key")
        if not data.get("id"):
            raise ComputeError("Shadeform accepted the SSH key but returned no id: run `labagent compute ssh-keys --cloud shadeform` to find it")
        return SshKey(id=str(data["id"]), name=name, fingerprint=want), True

    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        if not NAME_RE.fullmatch(spec.name):
            raise ComputeError("the host name must be 1 to 50 letters, digits, - or _")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        menu = "; ".join(f"vendor={o.vendor} region={o.region} ${o.hourly_usd:.2f}/h" for o in offers[:5]) or "none available right now"

        def refuse(why: str) -> ComputeError:
            return ComputeError(f"{why}. Available {spec.instance_type}: {menu}. See all: labagent compute offers --cloud {self.id} --type {spec.instance_type}")
        if not spec.vendor or not spec.region:
            raise refuse("set vendor and region")
        match = [o for o in offers if o.vendor.lower() == spec.vendor.lower() and o.region == spec.region]
        if not match:
            raise refuse(f"{spec.instance_type} is not available on {spec.vendor} in {spec.region}")
        if spec.os_image and spec.os_image not in match[0].os_options:
            raise ComputeError(f"image '{spec.os_image}' is not offered there. Offered: {', '.join(match[0].os_options) or 'none listed'}")
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


from .lambda_cloud import LambdaProvider  # noqa: E402  (after the names it imports from this module exist)

PROVIDERS: Dict[str, Callable[[], Provider]] = {"shadeform": ShadeformProvider, "lambda": LambdaProvider}


def make(provider_id: str) -> Provider:
    if provider_id not in PROVIDERS:
        raise ComputeError(f"unknown cloud '{provider_id}'. Available: {', '.join(sorted(PROVIDERS))}")
    return PROVIDERS[provider_id]()


def _shown(path: Path) -> str:
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home) else str(path)


def catalog() -> List[Dict[str, Any]]:
    """Every cloud this install can talk to, and whether its key is set. Reads no network and never returns a key."""
    out = []
    for cid in sorted(PROVIDERS):
        what = PROVIDERS[cid]
        env_var = getattr(what, "env_var", "")
        key_file = getattr(what, "key_file", "")
        key, source, warnings = find_secret(cid, env_var, key_file=key_file) if env_var else (None, None, [])
        creds = {"configured": bool(key), "source": source, "env_var": env_var or None, "file": _shown(credentials_path()), "section": cid}
        if key_file:
            creds["key_file"] = _shown(keys_dir() / key_file)
        out.append({"id": cid, "title": getattr(what, "title", "") or cid, "capabilities": list(getattr(what, "capabilities", ())),
                    "credentials": creds, "warnings": warnings})
    return out
