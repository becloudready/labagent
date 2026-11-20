"""RunPod as a compute provider. API v2: https://docs.runpod.io/api-reference-v2/overview (the v1 REST API and rest.runpod.io are gone).

What differs from Shadeform, Lambda and Nebius, and how it is handled here:

- A RunPod machine is a POD: a Docker CONTAINER on a host, not a VM. Every offer has `kind="container"`. A pod has no Docker daemon of its own and
  cannot change GPU state (`nvidia-smi -mig 1` answers "Insufficient Permissions", seen on a real pod 2026-11-19), so the gpu-notebooks pack, which needs
  root, Docker with the NVIDIA runtime and MIG, skips these offers. This provider is for `labagent compute ...` and for packs that can work inside a container.
- A pod is placed by GPU type, count and cloud (SECURE or COMMUNITY), optionally in named data centers. The offer's `instance_type` is RunPod's GPU id
  ("NVIDIA GeForce RTX 4090"); the familiar shapes work too: `H100` is one H100 GPU, `H100x8` is eight, `RTX4090` one RTX 4090. The `region` is a data center id
  such as `US-KS-2`. The `vendor` is `runpod-secure` or `runpod-community`, the cloud the price belongs to.
- Prices and stock come from `GET /v2/catalog/gpus?include=AVAILABILITY&product=POD`: USD per GPU per hour, so the offer price is that times the GPU count.
  One call answers for one GPU count, so a query for 8 GPUs asks for 8.
- SSH keys are plain strings in the account (`GET` and `PUT /v2/account/ssh-keys`), with no ids. The id used here is the key's fingerprint. `PUT` REPLACES
  the whole list, so adding a key sends the keys already there plus the new one, and is never done blind.
- A key reaches a pod through the `PUBLIC_KEY` environment variable that RunPod's official images turn into an authorized key (`startSsh`). The pod is
  reached by `ssh.direct` (a public IP and mapped port for 22/tcp), never the `ssh.proxy`, which carries only an interactive shell.
- There is NO automatic deletion (pods have no time limit), so `hours` cannot be enforced by the cloud. `auto_delete = False` makes the pack say so.
  A pod bills until it is terminated. Terminating deletes its container disk.
- The pod's "tags" field is not accepted by the create call as far as the docs show, so labagent's tags travel in the environment variable LABAGENT_TAGS and
  `find` matches on it.

Not proven against a live account (as of 2026-11-19): every response shape below is from RunPod's published API reference, checked with a fake. The first
real run should be a short one on the cheapest pod.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .errors import ComputeError
from .providers import (NAME_RE, USER_AGENT, ApiError, Host, HostSpec, Http, Offer, OfferQuery, Provider, SshKey, credentials_path,
                        fingerprint_of, read_secret)

BASE = "https://api.runpod.io"
SSH_USER = "root"
DEFAULT_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"     # the image in RunPod's own create-a-pod example
DISK_GB = 50
TAG_ENV = "LABAGENT_TAGS"
VENDORS = {"SECURE": "runpod-secure", "COMMUNITY": "runpod-community"}
GONE = ("deleting", "deleted")
# Data center ids start with the country: "US-KS-2", "EU-RO-1", "CA-MTL-1", "AP-JP-1", "EUR-IS-1", "OC-AU-1".
_AREA = {"US": "north-america", "CA": "north-america", "EU": "europe", "EUR": "europe", "AP": "asia-pacific", "OC": "asia-pacific"}


def runpod_area(data_center: str) -> str:
    """europe, north-america or asia-pacific from a data center id like US-KS-2, or "" when it does not say."""
    return _AREA.get(str(data_center or "").split("-")[0].upper(), "")


def family_of(name: str, vram_gb: int = 0) -> str:
    """The GPU family labagent uses elsewhere ('H100', 'RTX4090', 'A100_80G') from RunPod's name ('H100 SXM', 'RTX 4090', 'A100 PCIe')."""
    words = [w for w in re.split(r"[\s_-]+", str(name or "").strip()) if w and w.lower() != "tesla"]
    family = "".join(w for i, w in enumerate(words) if i == 0 or not re.fullmatch(r"(?i)(pcie|sxm\d*|nvl|hbm3e?|oam|\d+gb)", w))
    return "A100_80G" if family.upper() == "A100" and vram_gb == 80 else family      # the name Shadeform and the rest of labagent use for it


def _wanted(want: Optional[str], gpu_id: str, family: str, count: int) -> bool:
    """RunPod's own id, or the shape other clouds use: H100 is one GPU, H100x8 is eight."""
    if not want:
        return True
    w = want.strip().lower()
    return w in (gpu_id.lower(), f"{family.lower()}x{count}") or (count == 1 and w == family.lower())


def _same(value: Any, wanted: Optional[str]) -> bool:
    return wanted is None or str(value or "").lower() == wanted.lower()


def make_http(api_key: str) -> Http:
    def call(method: str, path: str, body: Optional[dict] = None) -> Any:
        req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https endpoint)
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")[:400])
        except (urllib.error.URLError, OSError):
            raise ComputeError("cannot reach api.runpod.io (network error)")
        return json.loads(raw) if raw.strip() else {}
    return call


class RunpodProvider(Provider):
    """RunPod rents GPU pods (containers) by the hour."""

    id = "runpod"
    title = "RunPod"
    capabilities = ("offers", "ssh_keys", "launch")
    env_var = "RUNPOD_API_KEY"
    key_file = "runpod_api_key"                     # ~/.labagent/keys/runpod_api_key holds just the key
    auto_delete = False
    builtin_jupyter = True                          # RunPod's official images start JupyterLab on 8888 when asked (startJupyter), behind its HTTPS proxy
    key_registry = False                            # the key is put in the pod's PUBLIC_KEY when it is created, so nothing in the account is changed

    def __init__(self, api_key: Optional[str] = None, http: Optional[Http] = None):
        self.warnings: List[str] = []
        if http is None:
            key = api_key
            if not key:
                key, self.warnings = read_secret("runpod", self.env_var, key_file=self.key_file)
            if not key:
                raise ComputeError(f"no RunPod API key: set {self.env_var}, or put it under [runpod] in {credentials_path()}, "
                                   f"or in ~/.labagent/keys/{self.key_file}")
            http = make_http(key)
        self._http = http

    # ---- plumbing ----
    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        try:
            return self._http(method, path, body)
        except ApiError as e:
            if e.status in (401, 403):
                why = ""
                try:
                    why = str(json.loads(e.body).get("detail") or "")[:200]           # e.g. "this API key does not grant catalog:read"
                except (ValueError, AttributeError):
                    pass
                raise ComputeError(f"RunPod rejected the API key (HTTP {e.status})" + (f": {why}" if why else " or it lacks the scope this call needs") +
                                   f". Check {self.env_var}, ~/.labagent/credentials or ~/.labagent/keys/{self.key_file}")
            if e.status == 429:
                raise ComputeError("RunPod says too many requests (HTTP 429): wait a minute and try again")
            raise

    @staticmethod
    def _fail(e: ApiError, what: str) -> ComputeError:
        detail = e.body
        try:
            j = json.loads(e.body)
            detail = " ".join(str(x) for x in (j.get("title"), j.get("detail"), *(j.get("errors") or [])) if x) or detail     # application/problem+json
        except (ValueError, AttributeError):
            pass
        return ComputeError(f"RunPod could not {what} ({e}): {str(detail)[:300]}")

    @staticmethod
    def _host(d: Dict[str, Any]) -> Host:
        raw = str(d.get("status") or "PROVISIONING").upper()
        status = {"PROVISIONING": "pending", "STARTING": "pending", "RUNNING": "active", "TERMINATED": "deleted"}.get(raw, raw.lower())
        direct = (d.get("ssh") or {}).get("direct") or {}
        if status == "active" and not direct.get("host"):
            status = "pending"                      # running, but the public port for SSH is not assigned yet
        tags = [t for t in str((d.get("env") or {}).get(TAG_ENV) or "").split(",") if t]
        return Host(id=str(d["id"]), name=d.get("name") or "", status=status, ip=direct.get("host") or None,
                    ssh_user=direct.get("username") or SSH_USER, ssh_port=direct.get("port") or None, tags=tags)

    # ---- offers ----
    def offers(self, query: OfferQuery) -> List[Offer]:
        if query.kind not in (None, "", "container"):
            return []                               # a pod is a container: no VM and no bare metal here
        shape = re.fullmatch(r"(?i)[A-Za-z0-9_]+x(\d+)", (query.instance_type or "").strip())
        count = query.num_gpus or (int(shape.group(1)) if shape else 1)          # H100x8 means eight GPUs
        clouds = [c for c, v in VENDORS.items() if _same(v, query.vendor)]
        out: List[Offer] = []
        for cloud in clouds:
            q = urllib.parse.urlencode({"include": "AVAILABILITY", "product": "POD", "count": count, "cloud": cloud})
            try:
                data = self._call("GET", "/v2/catalog/gpus?" + q) or {}
            except ApiError as e:
                raise self._fail(e, "list GPU types")
            for g in data.get("gpus") or []:
                gid, vram = str(g.get("id") or ""), int(g.get("memory") or 0)
                family = family_of(str(g.get("name") or gid), vram)
                per_gpu = (g.get("price") or {}).get(cloud.lower())
                max_count = int((g.get("maxCount") or {}).get(cloud.lower()) or 0)
                if not gid or per_gpu is None or count > max_count or not g.get(cloud.lower(), True):
                    continue
                price = round(float(per_gpu) * count, 2)
                if not (_same(family, query.gpu_type) and _wanted(query.instance_type, gid, family, count) and (query.max_price is None or price <= query.max_price)):
                    continue
                centers = [(str(c.get("id") or ""), str(c.get("name") or ""), str(c.get("availability") or "NONE").upper() != "NONE") for c in g.get("dataCenters") or []]
                if not centers:
                    centers = [("", "", str(g.get("availability") or "NONE").upper() != "NONE")]
                for cid, cname, available in centers:
                    if (query.available_only and not available) or (query.region and not _same(cid, query.region)):
                        continue
                    if query.area not in (None, "", "any") and runpod_area(cid) != query.area:
                        continue
                    out.append(Offer(instance_type=gid, vendor=VENDORS[cloud], region=cid, hourly_usd=price, available=available, region_name=cname,
                                     gpu_type=family, num_gpus=count, vram_gb=vram, vcpus=int(g.get("minVcpuCount") or 0) * count,
                                     memory_gb=int(g.get("minRam") or 0) * count, storage_gb=DISK_GB, kind="container", os_options=[DEFAULT_IMAGE],
                                     area_hint=runpod_area(cid)))
        out.sort(key=lambda o: (o.hourly_usd, o.vendor, o.region, o.instance_type))
        return out

    # ---- ssh keys ----
    def _key_lines(self) -> List[str]:
        try:
            body = self._call("GET", "/v2/account/ssh-keys") or {}
        except ApiError as e:
            raise self._fail(e, "list SSH keys")
        return [str(k) for k in body.get("keys") or []]

    @staticmethod
    def _key(line: str) -> SshKey:
        fp = fingerprint_of(line)
        comment = " ".join(line.split()[2:])
        return SshKey(id=fp or line[:40], name=comment or (fp[:19] if fp else "key"), is_default=False, fingerprint=fp)

    def ssh_keys(self) -> List[SshKey]:
        return [self._key(l) for l in self._key_lines() if l.strip()]       # only the public half exists here

    def add_ssh_key(self, name: str, public_key: str) -> Tuple[SshKey, bool]:
        want = fingerprint_of(public_key)
        if not want:
            raise ComputeError("that is not an OpenSSH public key line (expected: ssh-ed25519 AAAA... comment)")
        have = [l for l in self._key_lines() if l.strip()]
        for l in have:
            if fingerprint_of(l) == want:
                return self._key(l), False
        parts = public_key.split()
        line = " ".join(parts[:2] + [name]) if name else " ".join(parts[:2])
        # PUT replaces the list: send what is there plus the new key, so no key is ever lost. Read first, write once.
        try:
            self._call("PUT", "/v2/account/ssh-keys", {"keys": have + [line]})
        except ApiError as e:
            raise self._fail(e, "add the SSH key")
        return SshKey(id=want, name=name, fingerprint=want), True

    # ---- host lifecycle ----
    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        if not NAME_RE.fullmatch(spec.name):
            raise ComputeError("the host name must be 1 to 50 letters, digits, - or _")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        menu = "; ".join(f"vendor={o.vendor} region={o.region} ${o.hourly_usd:.2f}/h" for o in offers[:5]) or "none available right now"

        def refuse(why: str) -> ComputeError:
            return ComputeError(f"{why}. Available {spec.instance_type}: {menu}. See all: labagent compute offers --cloud runpod --type {spec.instance_type}")
        if not spec.vendor:
            raise refuse("set vendor (runpod-secure or runpod-community); region is optional, RunPod picks a data center")
        if spec.vendor.lower() not in VENDORS.values():
            raise refuse(f"vendor must be runpod-secure or runpod-community, not {spec.vendor}")
        match = [o for o in offers if o.vendor.lower() == spec.vendor.lower() and (not spec.region or o.region == spec.region)]
        if not match:
            raise refuse(f"{spec.instance_type} is not available on {spec.vendor}" + (f" in {spec.region}" if spec.region else ""))
        best = match[0]
        return {"hourly_cents": round(best.hourly_usd * 100), "os_options": best.os_options, "gpu_type": best.gpu_type, "instance_type": best.instance_type}

    def find(self, name: str, tag: str) -> Optional[Host]:
        cursor: Optional[str] = None
        for _ in range(50):
            path = "/v2/pods?limit=1000" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
            try:
                body = self._call("GET", path) or {}
            except ApiError as e:
                raise self._fail(e, "list pods")
            for d in body.get("pods") or []:
                h = self._host(d)
                if h.name == name and tag in h.tags and h.status not in GONE:
                    return h
            page = body.get("pagination") or {}
            cursor = page.get("nextCursor")
            if not page.get("hasNextPage") or not cursor:
                break
        return None

    def create(self, spec: HostSpec) -> str:
        if not spec.vendor or spec.vendor.lower() not in VENDORS.values():
            raise ComputeError("set vendor to runpod-secure or runpod-community")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type, vendor=spec.vendor, region=spec.region))
        if not offers:
            raise ComputeError(f"{spec.instance_type} has no capacity on {spec.vendor}" + (f" in {spec.region}" if spec.region else "") + " right now")
        best = offers[0]
        env: Dict[str, str] = {TAG_ENV: ",".join(spec.tags)}
        if spec.ssh_public_key:
            if not fingerprint_of(spec.ssh_public_key):
                raise ComputeError("ssh_public_key is not an OpenSSH public key line")
            env["PUBLIC_KEY"] = " ".join(spec.ssh_public_key.split()[:3])       # RunPod images turn this into an authorized key
        elif not any(k.id == spec.ssh_key_id for k in self.ssh_keys()):
            raise ComputeError(f"SSH key {spec.ssh_key_id or '(none)'} is not in your RunPod account: see labagent compute ssh-keys --cloud runpod")
        ports = ["22/tcp"] + [f"{int(x)}/http" for x in spec.ports]
        body: Dict[str, Any] = {"name": spec.name, "image": spec.os_image or DEFAULT_IMAGE, "gpu": {"id": best.instance_type, "count": best.num_gpus},
                                "cloud": next(c for c, v in VENDORS.items() if v == best.vendor), "env": env, "startSsh": True, "disk": DISK_GB}
        if spec.jupyter_password:                   # RunPod's own JupyterLab: it starts on 8888 from JUPYTER_PASSWORD, and the proxy needs 8888/http published
            env["JUPYTER_PASSWORD"] = spec.jupyter_password
            body["startJupyter"] = True
            if "8888/http" not in ports:
                ports.append("8888/http")
        body["ports"] = ports
        if spec.region:
            body["dataCenterIds"] = [spec.region]
        try:
            made = self._call("POST", "/v2/pods", body) or {}
        except ApiError as e:
            raise self._fail(e, "create the pod")
        if not made.get("id"):
            raise ComputeError("RunPod accepted the pod but returned no id: check the RunPod console for a new pod")
        return str(made["id"])

    def web_url(self, host_id: str, port: int) -> Optional[str]:
        """RunPod's HTTPS proxy for a port the pod published as `<port>/http`. The pattern is from RunPod's documentation as I remember it: not yet
        confirmed against a live pod."""
        return f"https://{urllib.parse.quote(str(host_id), safe='')}-{int(port)}.proxy.runpod.net"

    def get(self, host_id: str) -> Optional[Host]:
        try:
            d = self._call("GET", f"/v2/pods/{urllib.parse.quote(host_id, safe='')}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise self._fail(e, "read the pod")
        return self._host(d) if d else None

    def delete(self, host_id: str) -> None:
        try:
            self._call("DELETE", f"/v2/pods/{urllib.parse.quote(host_id, safe='')}")
        except ApiError as e:
            if e.status == 404:
                return                              # already gone
            raise self._fail(e, "terminate the pod")
