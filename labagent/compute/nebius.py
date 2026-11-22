"""Nebius AI Cloud as a compute provider. REST API: https://docs.nebius.com/rest-api (the OpenAPI file: https://api.nebius.cloud/openapi.json)

What differs from Shadeform and Lambda, and how it is handled here:

- A Nebius PROJECT belongs to one region (eu-north1, us-central1, ...). Everything is created inside the project, so the project you configure
  decides the region: set NEBIUS_PROJECT_ID (or put it in ~/.labagent/keys/nebius_project_id). To rent in another region, use that region's project.
- Instance types are two names, a platform and a preset (`gpu-h100-sxm` and `1gpu-16vcpu-200gb`). They are written `platform/preset`. The familiar
  shapes work too: `H100` is the one-GPU H100 preset and `H100x8` the eight-GPU one, so `instance_type=H100` means the same thing on every cloud.
- There is NO SSH key list to add to. The key is installed on the VM at creation (cloud-init), for the user `ubuntu`. So `ssh_key_id` means
  nothing here: the pack passes the PUBLIC half of your private key file (ssh_key_file, LABAGENT_SSH_KEY_FILE or the shared key) in `HostSpec.ssh_public_key`.
- There is NO automatic deletion (the API has no such setting), so `hours` cannot be enforced by the cloud. `auto_delete = False` makes the pack say
  so, and a launch that fails still deletes its own VM. Until you `destroy` it, a Nebius VM bills. The boot disk is created inside the instance
  request ("managed"), and deleting the instance deletes it too: nothing is left billing.
- Prices are not in the API. They are Nebius's public ON-DEMAND list prices per GPU-hour (https://nebius.com/prices, checked 2026-11-14), only for the GPU
  families listed in PRICE_PER_GPU_HOUR. A family not in the table is not offered, because a lab with no known price cannot respect a price ceiling.
  Nebius's own bill is what counts. The VM is never preemptible: a preemptible VM can be stopped in the middle of a class.
- Nebius shows no capacity, only what the project may use. An offer is "available" when the platform exists in the project; a launch that finds no
  capacity or quota fails fast with Nebius's own message, before anything is billed.
- The default security group of the project's network (all VMs are in it implicitly) lets all traffic in, so SSH, 80 and 443 reach the VM with nothing to open.
- Docker and its NVIDIA runtime are NOT confirmed on the CUDA images: the pack's installer checks for them and stops with a plain message,
  and a failed launch deletes the VM.

Credentials, first that exists (a token or the CLI is enough for one person; a service account is for a machine that runs unattended):
  1. a 12 hour IAM token: NEBIUS_IAM_TOKEN, `[nebius]` in ~/.labagent/credentials, or ~/.labagent/keys/nebius_iam_token
  2. a service account: NEBIUS_SERVICE_ACCOUNT_FILE or ~/.labagent/keys/nebius_service_account.json, a JSON file with
     {"service_account_id": "...", "public_key_id": "...", "private_key_file": "path to the PEM"}; labagent signs a 5 minute JWT with `openssl`
     and exchanges it for a token, as https://docs.nebius.com/rest-api/authentication describes
  3. the `nebius` command line tool, if it is installed and set up (`nebius iam get-access-token`)
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import ComputeError
from .providers import (NAME_RE, USER_AGENT, ApiError, Host, HostSpec, Http, Offer, OfferQuery, Provider, SshKey, credentials_path,
                        find_secret, fingerprint_of, keys_dir)

BASE = "https://api.nebius.cloud"
TOKEN_URL = "https://auth.eu.nebius.com:443/oauth2/token/exchange"
VENDOR = "nebius"
SSH_USER = "ubuntu"
DEFAULT_IMAGE = "ubuntu24.04-cuda13.0"
IMAGES = ("ubuntu24.04-cuda13.0", "ubuntu24.04-driverless")      # the two families named in Nebius's docs and examples; any other family name may be passed as `os`
BOOT_GIB = 300                                                    # boot disk: the lab images and Docker layers need room; it is deleted with the VM
PRICE_PER_GPU_HOUR = {"H100": 4.50, "H200": 5.40, "B200": 8.50}   # USD, on demand, from https://nebius.com/prices on 2026-11-14
# The preset list from the API gave no GPU memory on a real project (2026-11-14), so use the cards' known memory. B200 is left unknown (0) rather than guessed.
VRAM_GB = {"H100": 80, "H200": 141}
TAG_MAX = 63
SERVICE_ACCOUNT_FILE = "nebius_service_account.json"
OPERATION_WAIT_S = 8                                              # a create that cannot work (quota, no capacity) fails within seconds: say so now
_AREA = {"eu": "europe", "uk": "europe", "us": "north-america", "ca": "north-america", "ap": "asia-pacific"}


def nebius_area(region: str) -> str:
    """europe, north-america or asia-pacific from a region like eu-north1, or "" (me-west1 is in none of them)."""
    return _AREA.get(str(region or "").split("-")[0], "")


def family_of(platform: str) -> str:
    """'H100' from 'gpu-h100-sxm'; '' for a platform with no GPU (cpu-d3, cpu-e2)."""
    m = re.match(r"gpu-([a-z0-9]+)", str(platform or ""))
    return m.group(1).upper() if m else ""


def _label(text: str) -> str:
    """A label value Nebius accepts for sure: lower case letters, digits, - and _, at most 63."""
    return re.sub(r"[^a-z0-9_-]+", "-", str(text).lower()).strip("-")[:TAG_MAX]


def _wanted(want: Optional[str], native: str, family: str, gpus: int) -> bool:
    """`platform/preset`, or the shape other clouds use: H100 is one GPU, H100x8 is eight."""
    if not want:
        return True
    w = want.strip().lower()
    return w in (native.lower(), f"{family.lower()}x{gpus}") or (gpus == 1 and w == family.lower())


def _same(value: Any, wanted: Optional[str]) -> bool:
    return wanted is None or str(value or "").lower() == wanted.lower()


# ---- credentials -----------------------------------------------------------------------------------------------------------

def service_account_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("NEBIUS_SERVICE_ACCOUNT_FILE") or str(keys_dir() / SERVICE_ACCOUNT_FILE)))


def _service_account() -> Optional[Dict[str, str]]:
    """The service account file, if there is one and it is complete. Never read to be shown: only its three facts are used."""
    p = service_account_path()
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ComputeError(f"{p} is not a JSON file. Expected: " + '{"service_account_id": "...", "public_key_id": "...", "private_key_file": "..."}')
    missing = [k for k in ("service_account_id", "public_key_id", "private_key_file") if not str(d.get(k) or "").strip()]
    if missing:
        raise ComputeError(f"{p} is missing: {', '.join(missing)}")
    key = Path(os.path.expanduser(str(d["private_key_file"])))
    if not key.is_absolute():
        key = p.parent / key
    return {"service_account_id": str(d["service_account_id"]).strip(), "public_key_id": str(d["public_key_id"]).strip(), "private_key_file": str(key)}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def sign_jwt(account: Dict[str, str], now: float, run: Callable = subprocess.run) -> str:
    """The 5 minute RS256 JWT Nebius exchanges for a token: kid = the public key id, iss and sub = the service account. Signed by `openssl`."""
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": account["public_key_id"]}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iss": account["service_account_id"], "sub": account["service_account_id"], "exp": int(now) + 300}, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    try:
        r = run(["openssl", "dgst", "-sha256", "-sign", account["private_key_file"]], input=signing_input, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise ComputeError("the `openssl` command is not installed, which is needed to sign the Nebius service account token request")
    except subprocess.TimeoutExpired:
        raise ComputeError("openssl took too long to sign the Nebius token request")
    if r.returncode != 0 or not r.stdout:
        why = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["no detail"]
        raise ComputeError(f"openssl could not sign with {os.path.basename(account['private_key_file'])}: {why[0][:160]}")
    return f"{head}.{body}.{_b64url(r.stdout)}"


def exchange_jwt(jwt: str) -> Dict[str, Any]:
    """Trade the JWT for an access token (12 hours). The JWT is a form field, never logged."""
    form = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                                   "subject_token": jwt, "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=form, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https endpoint)
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise ComputeError(f"Nebius refused the service account (HTTP {e.code}): check service_account_id, public_key_id and that the key has not expired")
    except (urllib.error.URLError, OSError, ValueError):
        raise ComputeError("cannot reach auth.eu.nebius.com (network error)")


def _cli() -> Optional[str]:
    """The `nebius` command: on PATH, else where its installer puts it (~/.nebius/bin), which a shell profile adds to PATH but a job does not."""
    return shutil.which("nebius") or _installed_cli()


def _installed_cli() -> Optional[str]:
    spot = Path.home() / ".nebius" / "bin" / "nebius"
    return str(spot) if spot.is_file() else None


def extra_credentials() -> Optional[str]:
    """Where a token could come from besides the environment and the key file: a service account file, or the nebius command line tool."""
    if service_account_path().is_file():
        return "service account file"
    if _cli() and (Path.home() / ".nebius").exists():
        return "nebius CLI"
    return None


class Auth:
    """Hands out an IAM token and renews it when it is about to run out. A token set by a person (12 hours) is used as it is."""

    def __init__(self, token: Optional[str] = None, run: Callable = subprocess.run, exchange: Callable = exchange_jwt, clock: Callable = time.time):
        self._fixed, self._run, self._exchange, self._clock = token, run, exchange, clock
        self._token: Optional[str] = None
        self._expires = 0.0
        self.warnings: List[str] = []

    def token(self) -> str:
        if self._fixed:
            return self._fixed
        if self._token and self._clock() < self._expires - 60:
            return self._token
        value, source, self.warnings = find_secret("nebius", "NEBIUS_IAM_TOKEN", key_file="nebius_iam_token")
        if value:
            self._fixed = value
            return value
        account = _service_account()
        if account:
            got = self._exchange(sign_jwt(account, self._clock(), self._run))
            if not got.get("access_token"):
                raise ComputeError("Nebius accepted the service account but returned no token")
            self._token, self._expires = str(got["access_token"]), self._clock() + float(got.get("expires_in") or 3600)
            return self._token
        cli = _cli()
        if cli:
            try:
                r = self._run([cli, "iam", "get-access-token"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                raise ComputeError("`nebius iam get-access-token` did not answer")
            tok = (r.stdout or "").strip()
            if r.returncode != 0 or not tok or "\n" in tok:
                raise ComputeError("`nebius iam get-access-token` gave no token: run `nebius profile list` and `nebius iam whoami` to see what is wrong")
            self._token, self._expires = tok, self._clock() + 11 * 3600
            return tok
        raise ComputeError("no Nebius credentials: set NEBIUS_IAM_TOKEN (`nebius iam get-access-token`, valid 12 hours), or put a service account in "
                           f"{service_account_path()}, or install and set up the `nebius` command line tool. Details: {credentials_path()}")


def make_http(auth: Auth) -> Http:
    def call(method: str, path: str, body: Optional[dict] = None) -> Any:
        req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + auth.token(), "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https endpoint)
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")[:600])
        except (urllib.error.URLError, OSError):
            raise ComputeError("cannot reach api.nebius.cloud (network error)")
        return json.loads(raw) if raw.strip() else {}
    return call


# ---- the provider ----------------------------------------------------------------------------------------------------------

class NebiusProvider(Provider):
    """Nebius rents GPU VMs by the hour inside a project, with the SSH key installed at creation."""

    id = "nebius"
    title = "Nebius"
    capabilities = ("offers", "launch")                # no "ssh_keys": there is no key list to read or add to
    env_var = "NEBIUS_IAM_TOKEN"
    key_file = "nebius_iam_token"                     # ~/.labagent/keys/nebius_iam_token holds just the token
    auto_delete = False
    key_registry = False                              # the key goes into the VM at creation, not into the account
    vendor = VENDOR                                   # NebiusDevlabProvider changes these three: same project, same platforms, a container instead of a VM
    offer_kind = "vm"
    offer_os = IMAGES

    @classmethod
    def extra_credentials(cls) -> Optional[str]:
        return extra_credentials()

    def __init__(self, api_key: Optional[str] = None, http: Optional[Http] = None, project_id: Optional[str] = None, sleep: Callable = time.sleep):
        self.warnings: List[str] = []
        self._sleep = sleep
        self._project_id = (project_id or "").strip() or None
        self._cache: Dict[str, Any] = {}
        if http is None:
            auth = Auth(token=api_key)
            auth.token()                               # fail now, with the three ways to log in, not on the first call
            self.warnings = auth.warnings
            http = make_http(auth)
        self._http = http

    # ---- plumbing ----
    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        try:
            return self._http(method, path, body)
        except ApiError as e:
            if e.status == 401:
                raise ComputeError("Nebius rejected the token (an IAM token lasts 12 hours): get a new one with `nebius iam get-access-token`, or use a service account")
            raise

    @staticmethod
    def _fail(e: ApiError, what: str) -> ComputeError:
        detail = e.body
        try:
            detail = json.loads(e.body).get("message") or detail
        except (ValueError, AttributeError):
            pass
        return ComputeError(f"Nebius could not {what} ({e}): {str(detail)[:300]}")

    @staticmethod
    def _gone(e: ApiError) -> bool:
        return e.status == 404 or '"code": 5' in e.body or '"code":5' in e.body or "not found" in e.body.lower() or "notfound" in e.body.lower()

    def _project(self) -> str:
        if not self._project_id:
            value, _, _ = find_secret("nebius_project", "NEBIUS_PROJECT_ID", key_file="nebius_project_id")
            self._project_id = value
        if not self._project_id:
            raise ComputeError("no Nebius project: set NEBIUS_PROJECT_ID (it looks like project-e00...; the project also decides the region) "
                               "or put it in ~/.labagent/keys/nebius_project_id")
        return self._project_id

    def _region(self) -> str:
        if "region" not in self._cache:
            try:
                d = self._call("GET", f"/iam/v2/projects/{urllib.parse.quote(self._project(), safe='')}") or {}
            except ApiError as e:
                raise self._fail(e, "read the project")
            self._cache["region"] = str((d.get("spec") or {}).get("region") or "")
        return self._cache["region"]

    def _list(self, path: str, extra: str = "") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token: Optional[str] = None
        for _ in range(50):
            q = f"?parentId={urllib.parse.quote(self._project(), safe='')}&pageSize=100{extra}" + (f"&pageToken={urllib.parse.quote(token)}" if token else "")
            body = self._call("GET", path + q) or {}
            out.extend(body.get("items") or [])
            token = body.get("nextPageToken")
            if not token:
                break
        return out

    def _subnet(self) -> str:
        if "subnet" not in self._cache:
            chosen, _, _ = find_secret("nebius_subnet", "NEBIUS_SUBNET_ID", key_file="nebius_subnet_id")
            if not chosen:
                try:
                    subnets = self._list("/vpc/v1/subnets")
                except ApiError as e:
                    raise self._fail(e, "list the project's subnets")
                best = next((s for s in subnets if (s.get("status") or {}).get("default")), subnets[0] if subnets else None)
                chosen = ((best or {}).get("metadata") or {}).get("id")
            if not chosen:
                raise ComputeError("the Nebius project has no network subnet to put the VM in. Create the default network (`nebius vpc network create-default`) "
                                   "or set NEBIUS_SUBNET_ID")
            self._cache["subnet"] = chosen
        return self._cache["subnet"]

    @staticmethod
    def _host(d: Dict[str, Any]) -> Host:
        meta, status = d.get("metadata") or {}, d.get("status") or {}
        state = str(status.get("state") or "UNSPECIFIED").upper()
        mapped = {"RUNNING": "active", "STOPPING": "stopped", "STOPPED": "stopped", "DELETING": "deleting", "ERROR": "error"}.get(state, "pending")
        ip = None
        for nic in status.get("networkInterfaces") or []:
            addr = ((nic.get("publicIpAddress") or {}).get("address") or "").split("/")[0]
            if addr:
                ip = addr
                break
        return Host(id=str(meta.get("id") or ""), name=str(meta.get("name") or ""), status=mapped, ip=ip, ssh_user=SSH_USER, ssh_port=22,
                    tags=[str(v) for v in (meta.get("labels") or {}).values()])

    # ---- offers ----
    def offers(self, query: OfferQuery) -> List[Offer]:
        region = self._region()
        if query.region and not _same(region, query.region):
            return []                                  # a project is in one region
        if query.area not in (None, "", "any") and nebius_area(region) != query.area:
            return []
        if not _same(self.vendor, query.vendor) or query.kind not in (None, "", self.offer_kind):
            return []
        try:
            platforms = self._list("/compute/v1/platforms")
        except ApiError as e:
            raise self._fail(e, "list the platforms")
        out: List[Offer] = []
        for p in platforms:
            name = str((p.get("metadata") or {}).get("name") or "")
            spec = p.get("spec") or {}
            family = family_of(name)
            if family not in PRICE_PER_GPU_HOUR:
                continue                                # no GPU, or a GPU with no known price
            for pr in spec.get("presets") or []:
                res = pr.get("resources") or {}
                gpus = int(res.get("gpuCount") or 0)
                if gpus < 1:
                    continue
                native = f"{name}/{pr.get('name')}"
                price = round(PRICE_PER_GPU_HOUR[family] * gpus, 2)
                if not (_same(family, query.gpu_type) and (query.num_gpus is None or gpus == query.num_gpus) and _wanted(query.instance_type, native, family, gpus)
                        and (query.max_price is None or price <= query.max_price)):
                    continue
                vram = int(spec.get("gpuMemoryGibibytes") or (int(res.get("gpuMemoryGibibytes") or 0) // gpus)) or VRAM_GB.get(family, 0)
                out.append(Offer(instance_type=native, vendor=self.vendor, region=region, hourly_usd=price, available=True, region_name=region, gpu_type=family,
                                 num_gpus=gpus, vram_gb=vram, vcpus=int(res.get("vcpuCount") or 0), memory_gb=int(res.get("memoryGibibytes") or 0),
                                 storage_gb=BOOT_GIB, interconnect="SXM" if name.endswith("sxm") else "", kind=self.offer_kind, os_options=list(self.offer_os),
                                 area_hint=nebius_area(region)))
        out.sort(key=lambda o: (o.hourly_usd, o.region, o.instance_type))
        return out

    # ---- ssh keys: there is no list ----
    def ssh_keys(self) -> List[SshKey]:
        raise ComputeError("Nebius has no SSH key list to read: the key is installed on each VM when it is created, from the private key file you launch with "
                           "(ssh_key_file, LABAGENT_SSH_KEY_FILE, or the shared key ~/.labagent/keys/labagent_ed25519). Make that file with "
                           "`labagent compute ssh-key-add --cloud all --name labagent`")

    def add_ssh_key(self, name: str, public_key: str) -> Tuple[SshKey, bool]:
        return self.ssh_keys()                          # raises with the same explanation

    # ---- host lifecycle ----
    def validate(self, spec: HostSpec) -> Dict[str, Any]:
        if not NAME_RE.fullmatch(spec.name):
            raise ComputeError("the host name must be 1 to 50 letters, digits, - or _")
        if not spec.ssh_public_key or not fingerprint_of(spec.ssh_public_key):
            raise ComputeError("a Nebius VM gets its SSH key when it is created, so a public key is needed. Make one: labagent compute ssh-key-add --cloud all --name labagent")
        region = self._region()
        if spec.region and not _same(region, spec.region):
            raise ComputeError(f"the Nebius project {self._project()} is in {region}, not {spec.region}. Use a project in that region (NEBIUS_PROJECT_ID)")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        if not offers:
            menu = "; ".join(f"{o.instance_type} ${o.hourly_usd:.2f}/h" for o in self.offers(OfferQuery())[:6]) or "none"
            raise ComputeError(f"{spec.instance_type} is not offered in the Nebius project's region {region}. Offered there: {menu}. "
                               f"See all: labagent compute offers --cloud nebius")
        if spec.os_image and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", spec.os_image):
            raise ComputeError(f"image family '{spec.os_image}' does not look like a Nebius image family, for example {DEFAULT_IMAGE}")
        best = offers[0]
        return {"hourly_cents": round(best.hourly_usd * 100), "os_options": best.os_options, "gpu_type": best.gpu_type, "instance_type": best.instance_type}

    def find(self, name: str, tag: str) -> Optional[Host]:
        try:
            items = self._list("/compute/v1/instances")
        except ApiError as e:
            raise self._fail(e, "list instances")
        for d in items:
            h = self._host(d)
            if h.name == name and _label(tag) in h.tags and h.status not in ("deleting", "deleted"):
                return h
        return None

    @staticmethod
    def cloud_init(public_key: str) -> str:
        """Cloud-init that makes the user `ubuntu` (not root or admin, which Nebius refuses) with this one key and no password."""
        line = " ".join(str(public_key).split()[:3])
        return ("#cloud-config\nusers:\n  - name: " + SSH_USER + "\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    lock_passwd: true\n"
                "    ssh_authorized_keys:\n      - " + json.dumps(line) + "\n")

    def create(self, spec: HostSpec) -> str:
        if not spec.ssh_public_key or not fingerprint_of(spec.ssh_public_key):
            raise ComputeError("no SSH public key to install on the VM")
        offers = self.offers(OfferQuery(instance_type=spec.instance_type))
        if not offers:
            raise ComputeError(f"{spec.instance_type} is not offered in the Nebius project's region")
        platform, preset = offers[0].instance_type.split("/", 1)
        project = self._project()
        body = {
            "metadata": {"parentId": project, "name": spec.name, "labels": {f"labagent-{i}": _label(t) for i, t in enumerate(spec.tags)}},
            "spec": {
                "resources": {"platform": platform, "preset": preset},
                "networkInterfaces": [{"name": "eth0", "subnetId": self._subnet(), "ipAddress": {}, "publicIpAddress": {}}],
                "bootDisk": {"attachMode": "READ_WRITE", "deviceId": "boot-disk", "managedDisk": {
                    "name": f"{spec.name}-boot"[:63],
                    "spec": {"type": "NETWORK_SSD", "sizeGibibytes": str(BOOT_GIB), "blockSizeBytes": "4096",
                             "sourceImageFamily": {"imageFamily": spec.os_image or DEFAULT_IMAGE}}}},
                "cloudInitUserData": self.cloud_init(spec.ssh_public_key),
                "recoveryPolicy": "FAIL",              # a VM that fails is reported, not silently restarted while it bills
                "reservationPolicy": {"policy": "FORBID"},
            },
        }
        try:
            op = self._call("POST", "/compute/v1/instances", body) or {}
        except ApiError as e:
            raise self._fail(e, "create the instance")
        host_id = str(op.get("resourceId") or "")
        failure = self._operation_failure(str(op.get("id") or ""), op)
        if failure:
            raise ComputeError(f"Nebius could not create the instance: {failure}")
        if not host_id:
            found = self.find(spec.name, spec.tags[-1] if spec.tags else "")
            if not found:
                raise ComputeError("Nebius accepted the request but returned no instance id: check the Nebius console for a new instance named " + spec.name)
            host_id = found.id
        return host_id

    def _operation_failure(self, op_id: str, op: Dict[str, Any]) -> str:
        """The error text if the create operation has already failed (no quota, no capacity), else "". Waits a few seconds for it to say."""
        waited = 0
        while True:
            status = op.get("status") or {}
            if op.get("finishedAt") or status:          # finished: it failed only if its status carries an error code
                return "" if not status.get("code") else str(status.get("message") or "failed")[:300]
            if not op_id or waited >= OPERATION_WAIT_S:
                return ""
            self._sleep(2)
            waited += 2
            try:
                op = self._call("GET", f"/compute/v1/instances/operations/{urllib.parse.quote(op_id, safe='')}") or {}
            except ApiError:
                return ""                               # cannot tell: the pack's own wait will see what the VM does

    def get(self, host_id: str) -> Optional[Host]:
        try:
            d = self._call("GET", f"/compute/v1/instances/{urllib.parse.quote(host_id, safe='')}")
        except ApiError as e:
            if self._gone(e):
                return None
            raise self._fail(e, "read the instance")
        return self._host(d) if d and (d.get("metadata") or {}).get("id") else None

    def delete(self, host_id: str) -> None:
        try:
            self._call("DELETE", f"/compute/v1/instances/{urllib.parse.quote(host_id, safe='')}")
        except ApiError as e:
            if self._gone(e):
                return                                  # already gone
            raise self._fail(e, "delete the instance")
