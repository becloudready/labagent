"""Lambda Cloud (formerly Lambda Labs) as a compute provider. API reference: https://docs.lambda.ai/api/cloud

What differs from Shadeform, and how it is handled here:

- Lambda is one datacenter company, so `vendor` means nothing: it is always `lambda` and is ignored on launch.
- Instance types are Lambda's own names (`gpu_1x_h100_pcie`). The familiar shapes work too: `H100` is the cheapest one-GPU H100 in the
  region, `H100x8` the eight-GPU one, so `instance_type=H100 location=north-america` means the same thing on every cloud.
- An SSH key is chosen by NAME at launch. The pack's `ssh_key_id` is the key's id, and the provider looks the name up.
- There is NO automatic deletion (the API has no such setting), so `hours` cannot be enforced by the cloud. `auto_delete = False` makes
  the pack say so, and a launch that fails still deletes its own VM. Until you `destroy` it, a Lambda VM bills.
- A new Lambda account lets in only SSH and ping. The labs are served on 80 and 443, so a launch attaches a firewall ruleset that opens
  exactly those two TCP ports (one per region, named `labagent-web-<region>`, created on first use and reused after). It is never
  deleted by labagent, because other instances may share it, and the account's global rules are never touched.
- Only x86_64 types are offered: the lab image is built for it, so the ARM GH200 is left out.
- Lambda boots a Ubuntu image as user `ubuntu`. The default image is Lambda Stack 24.04 (NVIDIA driver and CUDA). Docker and its NVIDIA runtime are NOT confirmed there:
  the pack's installer checks for them and stops with a plain message, and a failed launch deletes the VM.
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

BASE = "https://cloud.lambda.com"                  # cloud.lambda.ai and cloud.lambdalabs.com still answer but are marked deprecated
VENDOR = "lambda"
SSH_USER = "ubuntu"
DEFAULT_IMAGE = "lambda-stack-24-04"
WEB_RULESET = "labagent-web"
TAG_MAX = 128                                      # Lambda's limit on a tag value; the pack's tag is cut to it on both create and find
IMAGES = ("lambda-stack-24-04", "gpu-base-24-04", "lambda-stack-22-04", "gpu-base-22-04")      # families with CUDA and Docker, x86_64

# A region's code starts with where it is. Lambda's descriptions ("Utah, USA", "Germany") carry no country code, so the code is used.
_REGION_AREA = {"us": "north-america", "ca": "north-america", "mx": "north-america", "europe": "europe", "asia": "asia-pacific", "australia": "asia-pacific"}


def lambda_area(region: str) -> str:
    """europe, north-america or asia-pacific from a region code like us-west-3, or "" (me-west-1 is not in any of them)."""
    return _REGION_AREA.get(str(region or "").split("-")[0], "")


def gpu_of(description: str) -> Tuple[str, int, str]:
    """('H100', 80, 'SXM5') from 'H100 (80 GB SXM5)'; ('', 0, '') when it does not look like that. The family is what `--gpu` matches."""
    m = re.match(r"\s*(?:Tesla\s+)?([A-Za-z0-9]+(?:\s[A-Za-z0-9]+)?)\s*\((\d+)\s*GB\s*([A-Za-z0-9]*)", description or "")
    if not m:
        return "", 0, ""
    family, gb = m.group(1).replace(" ", ""), int(m.group(2))
    if family == "A100" and gb == 80:
        family = "A100_80G"                         # the name Shadeform and the rest of labagent use for it
    return family, gb, m.group(3)


def _wanted(want: Optional[str], name: str, gpu_type: str, gpus: int) -> bool:
    """Lambda's own name (gpu_1x_h100_pcie), or the shape other clouds use: H100 is one GPU, H100x8 is eight."""
    if not want:
        return True
    w, family = want.strip().lower(), gpu_type.lower()
    return w in (name.lower(), f"{family}x{gpus}") or (gpus == 1 and w == family)


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
            raise ComputeError("cannot reach cloud.lambda.com (network error)")
        return json.loads(raw) if raw.strip() else {}
    return call


class LambdaProvider(Provider):
    """Lambda rents GPU VMs by the hour with one SSH key per instance."""

    id = "lambda"
    title = "Lambda"
    capabilities = ("offers", "ssh_keys", "launch")
    env_var = "LAMBDA_API_KEY"
    key_file = "lambda_api_key"                     # ~/.labagent/keys/lambda_api_key holds just the key
    auto_delete = False

    def __init__(self, api_key: Optional[str] = None, http: Optional[Http] = None):
        self.warnings: List[str] = []
        if http is None:
            key = api_key
            if not key:
                key, self.warnings = read_secret("lambda", self.env_var, key_file=self.key_file)
            if not key:
                raise ComputeError(f"no Lambda API key: set {self.env_var}, or put it under [lambda] in {credentials_path()}, "
                                   f"or in ~/.labagent/keys/{self.key_file}")
            http = make_http(key)
        self._http = http

    # ---- plumbing ----
    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        try:
            return self._http(method, path, body)
        except ApiError as e:
            if e.status in (401, 403):
                raise ComputeError(f"Lambda rejected the API key: check {self.env_var}, ~/.labagent/credentials or ~/.labagent/keys/{self.key_file}")
            raise

    def _data(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        return (self._call(method, path, body) or {}).get("data")

    @staticmethod
    def _fail(e: ApiError, what: str) -> ComputeError:
        detail = e.body
        try:
            err = json.loads(e.body).get("error")
            if isinstance(err, dict):
                detail = " ".join(x for x in (err.get("message"), err.get("suggestion")) if x) or detail
        except (ValueError, AttributeError):
            pass
        return ComputeError(f"Lambda could not {what} ({e}): {str(detail)[:300]}")

    @staticmethod
    def _host(d: Dict[str, Any]) -> Host:
        raw = str(d.get("status") or "pending").lower()
        status = {"booting": "pending", "terminating": "deleting", "terminated": "deleted", "preempted": "deleted"}.get(raw, raw)
        return Host(id=str(d["id"]), name=d.get("name") or "", status=status, ip=d.get("ip") or None, ssh_user=SSH_USER, ssh_port=22,
                    tags=[str(t.get("value")) for t in d.get("tags") or [] if isinstance(t, dict)])

    # ---- offers ----
    def offers(self, query: OfferQuery) -> List[Offer]:
        try:
            data = self._data("GET", "/api/v1/instance-types") or {}
        except ApiError as e:
            raise self._fail(e, "list instance types")
        out: List[Offer] = []
        for name, v in data.items():
            t = v.get("instance_type") or {}
            specs = t.get("specs") or {}
            gpus = int(specs.get("gpus") or 0)
            if gpus < 1 or (t.get("architecture") or "x86_64") != "x86_64":
                continue                            # a CPU-only type, or ARM: the lab image is for x86_64 with a GPU
            family, vram, link = gpu_of(t.get("gpu_description") or "")
            price = int(t.get("price_cents_per_hour") or 0) / 100
            if not (_same(family, query.gpu_type) and (query.num_gpus is None or gpus == query.num_gpus) and _wanted(query.instance_type, name, family, gpus)
                    and _same(VENDOR, query.vendor) and query.kind in (None, "", "vm") and (query.max_price is None or price <= query.max_price)):
                continue
            regions = [(r.get("name") or "", r.get("description") or "", True) for r in v.get("regions_with_capacity_available") or []]
            if not regions and not query.available_only:
                regions = [("", "", False)]         # sold out everywhere: Lambda lists no region for it
            for rname, rdesc, available in regions:
                if query.region and not _same(rname, query.region):
                    continue
                if query.area not in (None, "", "any") and lambda_area(rname) != query.area:
                    continue
                out.append(Offer(instance_type=str(name), vendor=VENDOR, region=rname, hourly_usd=round(price, 2), available=available, region_name=rdesc,
                                 gpu_type=family, num_gpus=gpus, vram_gb=vram, vcpus=int(specs.get("vcpus") or 0), memory_gb=int(specs.get("memory_gib") or 0),
                                 storage_gb=int(specs.get("storage_gib") or 0), interconnect=link, kind="vm", os_options=list(IMAGES),
                                 area_hint=lambda_area(rname)))
        out.sort(key=lambda o: (o.hourly_usd, o.vendor, o.region, o.instance_type))
        return out

    # ---- ssh keys ----
    def ssh_keys(self) -> List[SshKey]:
        try:
            data = self._data("GET", "/api/v1/ssh-keys") or []
        except ApiError as e:
            raise self._fail(e, "list SSH keys")
        # Only the public half is read, and only for its fingerprint.
        return [SshKey(id=str(k["id"]), name=str(k.get("name") or ""), is_default=False, fingerprint=fingerprint_of(k.get("public_key") or ""))
                for k in data if k.get("id")]

    def add_ssh_key(self, name: str, public_key: str) -> Tuple[SshKey, bool]:
        want = fingerprint_of(public_key)
        if not want:
            raise ComputeError("that is not an OpenSSH public key line (expected: ssh-ed25519 AAAA... comment)")
        for k in self.ssh_keys():
            if k.fingerprint == want:
                return k, False
        line = " ".join(public_key.split()[:3])
        try:
            # With a public_key, Lambda stores it. Without one it would generate a pair and send the private half: never asked for.
            data = self._data("POST", "/api/v1/ssh-keys", {"name": name, "public_key": line}) or {}
        except ApiError as e:
            raise self._fail(e, "add the SSH key")
        if not data.get("id"):
            raise ComputeError("Lambda accepted the SSH key but returned no id: run `labagent compute ssh-keys --cloud lambda` to find it")
        return SshKey(id=str(data["id"]), name=name, fingerprint=want), True

    # ---- host lifecycle ----
    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        if not NAME_RE.fullmatch(spec.name):
            raise ComputeError("the host name must be 1 to 50 letters, digits, - or _")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        menu = "; ".join(f"region={o.region} {o.instance_type} ${o.hourly_usd:.2f}/h" for o in offers[:5]) or "none available right now"

        def refuse(why: str) -> ComputeError:
            return ComputeError(f"{why}. Available {spec.instance_type}: {menu}. See all: labagent compute offers --cloud lambda --type {spec.instance_type}")
        if not spec.region:
            raise refuse("set region (or location, to let labagent pick the cheapest)")
        match = [o for o in offers if o.region == spec.region]
        if not match:
            raise refuse(f"{spec.instance_type} is not available in {spec.region}")
        if spec.os_image and spec.os_image not in match[0].os_options:
            raise ComputeError(f"image '{spec.os_image}' is not offered. Offered: {', '.join(match[0].os_options)}")
        best = match[0]                             # cheapest first
        return {"hourly_cents": round(best.hourly_usd * 100), "os_options": best.os_options, "gpu_type": best.gpu_type, "instance_type": best.instance_type}

    def find(self, name: str, tag: str) -> Optional[Host]:
        token: Optional[str] = None
        for _ in range(50):                         # 50 pages of 100 is far more than any account holds
            path = "/api/v1/instances?page_size=100" + (f"&page_token={urllib.parse.quote(token)}" if token else "")
            try:
                body = self._call("GET", path) or {}
            except ApiError as e:
                raise self._fail(e, "list instances")
            for d in body.get("data") or []:
                h = self._host(d)
                if h.name == name and tag[:TAG_MAX] in h.tags and h.status not in ("deleting", "deleted"):
                    return h
            token = body.get("page_token")
            if not token:
                break
        return None

    def _web_ruleset(self, region: str) -> str:
        """Id of the ruleset that opens 80 and 443 in this region: found by name, else created. Nothing is billed by this, and it runs before the launch."""
        name = f"{WEB_RULESET}-{region}"[:64]
        try:
            for r in self._data("GET", "/api/v1/firewall-rulesets") or []:
                got = r.get("region")
                if r.get("name") == name and (got.get("name") if isinstance(got, dict) else got) == region and r.get("id"):
                    return str(r["id"])
            rules = [{"protocol": "tcp", "port_range": [port, port], "source_network": "0.0.0.0/0", "description": f"labagent labs: {what}"[:128]}
                     for port, what in ((80, "http, certificate check"), (443, "https, the labs"))]
            made = self._data("POST", "/api/v1/firewall-rulesets", {"name": name, "region": region, "rules": rules}) or {}
        except ApiError as e:
            raise self._fail(e, "set up the firewall rules that let the labs be reached (ports 80 and 443)")
        if not made.get("id"):
            raise ComputeError("Lambda created the firewall ruleset but returned no id: nothing was rented. Check Firewall in the Lambda console")
        return str(made["id"])

    def create(self, spec: HostSpec) -> str:
        key = next((k for k in self.ssh_keys() if k.id == spec.ssh_key_id), None)
        if key is None:
            raise ComputeError(f"SSH key {spec.ssh_key_id or '(none)'} is not in your Lambda account: see labagent compute ssh-keys --cloud lambda")
        if not spec.region:
            raise ComputeError("set region")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type, region=spec.region))
        if not offers:
            raise ComputeError(f"{spec.instance_type} has no capacity in {spec.region} right now")
        ruleset = self._web_ruleset(spec.region)          # before the launch: if this fails, nothing has been rented
        body = {"region_name": spec.region, "instance_type_name": offers[0].instance_type, "ssh_key_names": [key.name], "name": spec.name,
                "image": {"family": spec.os_image or DEFAULT_IMAGE}, "tags": [{"key": f"labagent-{i}", "value": t[:TAG_MAX]} for i, t in enumerate(spec.tags)],
                "firewall_rulesets": [{"id": ruleset}]}
        try:
            ids = (self._data("POST", "/api/v1/instance-operations/launch", body) or {}).get("instance_ids") or []
        except ApiError as e:
            raise self._fail(e, "launch the instance")
        if not ids:
            raise ComputeError("Lambda accepted the launch but returned no instance id: check the Lambda console for a new instance")
        return str(ids[0])

    def get(self, host_id: str) -> Optional[Host]:
        try:
            d = self._data("GET", f"/api/v1/instances/{urllib.parse.quote(host_id, safe='')}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise self._fail(e, "read the instance")
        return self._host(d) if d else None

    def delete(self, host_id: str) -> None:
        try:
            self._call("POST", "/api/v1/instance-operations/terminate", {"instance_ids": [host_id]})
        except ApiError as e:
            if e.status == 404 or "not-found" in e.body.lower() or "not found" in e.body.lower():
                return                              # already gone
            raise self._fail(e, "terminate the instance")
