"""gpu-notebooks: one JupyterLab per slice of a rented GPU (NVIDIA MIG), each in its own Docker container.
GPUs without MIG (GeForce, such as the RTX 5090) can instead be shared by several labs: mode=shared.

`launch` rents a GPU VM, splits every GPU into MIG slices, starts one container per slice with its own JupyterLab and random
token, puts an HTTPS front door in front, checks every lab, and prints a URL and login for each. The VM deletes itself after
`hours` (on Shadeform; Lambda has no such setting, so there the VM bills until `destroy`, and a failed launch still deletes its own VM).
`destroy` deletes only the VM this launch created.

Where things live: the VM gets /opt/labagent/start-labs.sh (setup.sh) and a systemd unit that rebuilds the slices and
containers at boot. The tokens are written to a 0600 credentials file on this machine and on the VM, and never to state or logs
except the final report that prints them for you.
"""
from __future__ import annotations

import csv
import os
import re
import secrets
import shlex
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Pack, PackContext, PackError, Param, Step, save_state, state_dir
from ...compute import keys as keyfiles, providers
from ...compute.providers import GONE, Host, HostSpec
from .remote import SshRunner

PACK_ID = "gpu-notebooks"
WAIT_S = 1200            # first boot of a GPU VM, including provider-side provisioning
WAIT_SSH_S = 300
WAIT_PUBLIC_S = 240      # certificate issuance for the HTTPS name
POLL_S = 10
MAX_LABS = 99            # lab names are lab-01 .. lab-99
REBOOT_RC = 75           # the VM script: MIG is on but needs a reboot to take effect (A100)
REBOOT_WAIT_S = 600
NO_MIG = ("RTX4090", "RTX5090", "A10", "A6000", "RTX6000", "V100")   # no MIG on these (GeForce, workstation, Volta), so mode=mig can never work. Checked before anything is rented
INSTALL_DIR = "/opt/labagent"
SCRIPT = f"{INSTALL_DIR}/start-labs.sh"


def host_name(name: str) -> str:
    return ("labagent-" + re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-"))[:50]


def host_tag(name: str) -> str:
    return f"labagent:{PACK_ID}:{name}"[:200]


def flag(value: Any) -> bool:
    """A yes/no param: the command line gives text ('false' is not False), a launch file gives true or false."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def lab_name(i: int) -> str:
    return f"lab-{i:02d}"


def lab_port(i: int) -> int:
    return 8800 + i


def default_domain(ip: str) -> str:
    return ip.replace(".", "-") + ".sslip.io"


def credentials_path(name: str) -> Path:
    return state_dir() / PACK_ID / f"{name}-credentials.csv"


def login_path(name: str) -> Path:
    return state_dir() / PACK_ID / f"{name}-ssh-login.csv"


def default_hostname(name: str) -> str:
    """The launch name, as a host name: lower case letters, digits and hyphens. It shows in the prompt of every screenshot."""
    h = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    return h or "gpu-labs"


def _known_hosts(name: str, host_id: str) -> Path:
    return state_dir() / PACK_ID / f"{name}-{host_id}.known_hosts"


def setup_script() -> str:
    return (Path(__file__).with_name("setup.sh")).read_text(encoding="utf-8")


class _LazyProviders:
    """Builds the real provider on first use, inside a step, so `--dry-run` needs no API key."""

    def __call__(self, provider_id: str):
        return providers.make(provider_id)


def _http_status(url: str, verify: bool) -> Optional[int]:
    ctx = None if verify else ssl._create_unverified_context()      # noqa: S323 (self-signed on purpose when tls=internal)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": providers.USER_AGENT})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 (any failure means: not reachable)
        return None


class GpuNotebooks(Pack):
    id = PACK_ID
    title = "GPU notebooks: one JupyterLab per GPU slice"
    summary = "Rent a GPU VM and give each MIG slice its own JupyterLab"
    aliases = ("gpu-labs",)
    platforms = ("shadeform", "lambda", "nebius")
    skip_containers = True                  # mode mig and shared need Docker and root on a VM; the container pack turns this off
    cost_tier = "metered"
    default_name = "gpu"
    params = [
        Param("cloud", "shadeform", f"Whose API to call: {', '.join(sorted(platforms))}, or auto: ask every cloud that has a key here and take the cheapest "
              "machine of this shape (its cloud must also have your SSH key). With auto leave vendor and region out; location narrows it"),
        Param("instance_type", "H100", "The cloud's GPU shape, e.g. H100 (one GPU) or H100x8 (eight). A cloud's own names also work: gpu_1x_h100_pcie (lambda), "
                                       "gpu-h100-sxm/1gpu-16vcpu-200gb (nebius: platform/preset)"),
        Param("vendor", None, "shadeform only: the datacenter company behind the offer, e.g. scaleway. Leave out to list what is available"),
        Param("region", None, "The region: on shadeform that vendor's, e.g. paris-france-1; on lambda e.g. us-west-3; on nebius the region of the project (NEBIUS_PROJECT_ID), e.g. eu-north1"),
        Param("location", "", "Leave vendor and region out and set this instead: any | europe | north-america | asia-pacific. "
                              "The cheapest available machine of this instance_type there is picked"),
        Param("max_price", 0, "Most you will pay, in dollars per hour. Used when picking by location: dearer offers are ignored. 0 = no limit"),
        Param("os", None, "VM image from the cloud's list, e.g. ubuntu24.04_cuda13.0_shade_os (default: the cloud's)"),
        Param("ssh_key_id", None, "Optional. Leave it out to use your one shared key: the launch finds it in the cloud account by the fingerprint of the private "
                                  "key file (make it once for every cloud: `labagent compute ssh-key-add --cloud all --name labagent`). Set it to pick a "
                                  "different key by id: see `labagent compute ssh-keys`. Never the account's default key, whose private half you do not hold. "
                                  "Not used on nebius, which has no key list: the key is installed on the VM when it is created"),
        Param("ssh_key_file", None, "The private key file to log in with. Default: the LABAGENT_SSH_KEY_FILE environment variable, then "
                                    "~/.labagent/keys/labagent_ed25519 if it exists. It is checked against the key in the account before anything is rented"),
        Param("keep_on_failure", "no", "yes | no. no deletes the VM when the launch fails, so a broken launch does not keep billing. "
                                       "yes leaves it running to debug (you pay until you destroy it, or `hours` ends)"),
        Param("hours", 4, "shadeform deletes the VM after this many hours (1 to 72), whatever happens to this tool. lambda and nebius have no such setting: "
                          "there it only sets the planned end shown in the report, and you must run destroy"),
        Param("mode", "mig", "mig: one hardware-isolated MIG slice per lab (H100, H200, A100). "
                             "shared: labs share the whole GPU with no isolation, for GPUs without MIG such as the RTX 5090 (needs labs)"),
        Param("mig_profile", "auto", "mig only. MIG slice size. auto = the smallest the GPU offers (1g.10gb on H100 and A100 80GB, 1g.5gb on A100 40GB). "
                                 "Others: 1g.20gb, 2g.20gb, 3g.40gb"),
        Param("labs", 0, "How many labs to start (mig: 0 = one per slice. shared: required, spread over the GPUs). At most 99)"),
        Param("image", "quay.io/jupyter/pytorch-notebook:cuda12-latest", "Docker image for each lab. One container per slice"),
        Param("tls", "letsencrypt", "letsencrypt (real certificate on <ip>.sslip.io) | internal (self-signed, browser warning) | none (plain http)"),
        Param("domain", None, "Name the labs are served on (default: <ip>.sslip.io). Its DNS must point at the VM"),
        Param("hostname", None, "Host name of the VM, shown in every shell prompt (default: the launch --name, e.g. nvidia-h100-hackathon)"),
        Param("login_user", "labagent", "A user to share with students: ssh with a password. Empty = do not create one"),
        Param("show_passwords", "yes", "yes | no. no keeps every lab token and the shell password out of the printed report (they stay in the credentials file, "
                                       "and a job with a public key returns the tokens sealed). The console always sets no"),
        Param("login_sudo", True, "Give login_user sudo and docker. WARNING: that is root, so one student can read every lab's token and stop other labs"),
    ]
    output_keys = ("cloud", "host_name", "host_id", "vendor", "region", "ip", "ssh_user", "ssh_port", "hourly_cents", "expires_utc", "gpu", "mode", "profile", "slices",
                   "domain", "base_url", "credentials_file", "public_ok", "hostname", "login_user", "login_file",
                   "ssh_key_id", "ssh_key_name", "ssh_key_fingerprint", "ssh_key_verified", "auto_delete")

    def services(self, profile):
        return {"provider": _LazyProviders(), "ssh": SshRunner}

    def outputs(self, state):
        """No tokens: those are in the credentials file and in the report."""
        out = {k: state[k] for k in self.output_keys if k in state}
        base = state.get("base_url")
        out["labs"] = {n: {"url": f"{base}/{n}/lab"} for n in state.get("labs", [])}
        return out

    # ---- helpers ----
    def _spec(self, ctx: PackContext) -> HostSpec:
        p = ctx.params
        try:
            hours = int(p["hours"])
        except (TypeError, ValueError):
            raise PackError("hours must be a whole number from 1 to 72")
        if not 1 <= hours <= 72:
            raise PackError("hours must be between 1 and 72")
        st = ctx.state                                  # a machine picked by `location` is remembered, so a rerun asks for the same one
        return HostSpec(name=host_name(ctx.name), instance_type=str(p["instance_type"]), vendor=p["vendor"] or st.get("vendor"),
                        region=p["region"] or st.get("region"), os_image=p["os"] or st.get("os"), ssh_key_id=p["ssh_key_id"] or st.get("ssh_key_id"), hours=hours,
                        tags=["labagent", host_tag(ctx.name)], ssh_public_key=st.get("ssh_public_key"))

    def _check_lab_params(self, ctx: PackContext) -> int:
        p = ctx.params
        if p["mode"] not in ("mig", "shared"):
            raise PackError("mode must be mig or shared")
        if p["location"] and p["location"] not in providers.LOCATIONS:
            raise PackError(f"location must be one of: {', '.join(providers.LOCATIONS)}")
        if not re.fullmatch(r"[0-9a-z.+]+", str(p["mig_profile"])):
            raise PackError("mig_profile must be auto, or look like 1g.10gb")
        if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", str(p["image"])):
            raise PackError("image must be a Docker image name such as quay.io/jupyter/pytorch-notebook:cuda12-latest")
        if p["tls"] not in ("letsencrypt", "internal", "none"):
            raise PackError("tls must be letsencrypt, internal or none")
        if p["show_passwords"] not in ("yes", "no"):
            raise PackError("show_passwords must be yes or no")
        if str(p["keep_on_failure"]).strip().lower() not in ("yes", "no", "true", "false", "1", "0", "on", "off"):
            raise PackError("keep_on_failure must be yes or no")
        try:
            if float(p["max_price"]) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise PackError("max_price must be a number of dollars per hour, or 0 for no limit")
        if p["domain"] and not re.fullmatch(r"[A-Za-z0-9.-]+", str(p["domain"])):
            raise PackError("domain must be a plain host name")
        if p["hostname"] and not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", str(p["hostname"])):
            raise PackError("hostname must be lower case letters, digits and hyphens, for example nvidia-h100-hackathon")
        if p["login_user"] and (not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", str(p["login_user"])) or p["login_user"] in ("root", "shadeform", "ubuntu")):
            raise PackError("login_user must be a new lower case user name (not root, ubuntu or shadeform)")
        try:
            n = int(p["labs"])
        except (TypeError, ValueError):
            raise PackError("labs must be a whole number")
        if not 0 <= n <= MAX_LABS:
            raise PackError(f"labs must be between 0 and {MAX_LABS}")
        if p["mode"] == "shared" and n < 1:
            raise PackError("mode=shared needs labs=N: how many labs share the GPU. Each one sees all of its memory, so keep it small, e.g. 4")
        return n

    @staticmethod
    def _cloud(ctx: PackContext) -> str:
        """The cloud this run uses. cloud=auto is decided once, by preflight, and kept in the state; until then it reads "auto"."""
        c = str(ctx.params["cloud"])
        return (ctx.state.get("cloud") or c) if c == "auto" else c

    def _provider(self, ctx: PackContext):
        cloud = self._cloud(ctx)
        if cloud == "auto":
            raise PackError("cloud=auto was not decided yet: no VM was created by this run. Destroy looks for it in every cloud")
        prov = ctx.services["provider"](cloud)
        for w in getattr(prov, "warnings", []):
            ctx.log(f"  warning: {w}")
        return prov

    def _runner(self, ctx: PackContext):
        st = ctx.state
        kh = _known_hosts(ctx.name, st["host_id"])
        kh.parent.mkdir(parents=True, exist_ok=True)
        return ctx.services["ssh"](st["ip"], st.get("ssh_user") or "shadeform", st.get("ssh_port") or 22, self._key_file(ctx), str(kh))

    @staticmethod
    def _key_file(ctx: PackContext) -> Optional[str]:
        """The private key to log in with: the parameter, else LABAGENT_SSH_KEY_FILE, else the shared key file if there is one. A job cannot set the
        parameter, the machine's owner sets the variable (or just keeps the shared key in its usual place)."""
        f = ctx.params.get("ssh_key_file") or os.environ.get("LABAGENT_SSH_KEY_FILE")
        if not f and os.path.isfile(keyfiles.default_key_file()):
            f = keyfiles.default_key_file()
        return os.path.expanduser(str(f)) if f else None

    def _check_ssh_key(self, ctx: PackContext, prov) -> Dict[str, Any]:
        """Before a VM exists: the key is in the account, and (when we hold the private file) the file really is its other half.
        A mismatch is the one failure that cannot be fixed after the VM is rented, and it is the most likely one.
        With no ssh_key_id the key is the one in the account whose fingerprint is the private file's: one key can serve every cloud."""
        p = ctx.params
        if not getattr(prov, "key_registry", True):
            return self._inline_key(ctx)
        key_id = str(p["ssh_key_id"] or "").strip()
        if not key_id:
            return self._find_key(ctx, prov)
        have = {k.id: k for k in prov.ssh_keys()}
        key = have.get(key_id)
        if key is None:
            menu = "; ".join(f"{k.id} {k.name}" for k in list(have.values())[:6]) or "none"
            raise PackError(f"ssh_key_id {key_id} is not in your {self._cloud(ctx)} account. Keys there: {menu}. "
                            f"See them: labagent compute ssh-keys --cloud {self._cloud(ctx)}")
        facts: Dict[str, Any] = {"ssh_key_id": key.id, "ssh_key_name": key.name, "ssh_key_fingerprint": key.fingerprint, "ssh_key_verified": False}
        f = self._key_file(ctx)
        if not f:
            ctx.log(f"  warning: no ssh_key_file (or LABAGENT_SSH_KEY_FILE), so ssh will try whatever keys this machine has. Key {key.name} ({key.id}) is not checked")
            return facts
        if not key.fingerprint:
            ctx.log(f"  warning: the cloud shows no public key for {key.name}, so {os.path.basename(f)} cannot be checked against it")
            return facts
        mine = ctx.services.get("private_fingerprint", keyfiles.private_fingerprint)(f)
        if mine != key.fingerprint:
            raise PackError(f"the SSH key file {os.path.basename(f)} ({mine}) is not the private half of key {key.name} ({key.fingerprint}). "
                            f"Nothing was rented. Use the file that matches ssh_key_id {key.id}, or an ssh_key_id that matches the file")
        ctx.log(f"  ssh key: {key.name} ({key.id}), {key.fingerprint}, matches {os.path.basename(f)}")
        facts["ssh_key_verified"] = True
        return facts

    def _inline_key(self, ctx: PackContext) -> Dict[str, Any]:
        """A cloud with no key list (Nebius): the key is put on the VM when it is created, so what is needed is the PUBLIC half of the private file we log in with.
        It is kept in state (a public key is not a secret), so the host step and a rerun ask for the same key."""
        cloud, f = self._cloud(ctx), self._key_file(ctx)
        if not f:
            raise PackError("no SSH key to log in with. Make one that every cloud shares: labagent compute ssh-key-add --cloud all --name labagent. "
                            f"{cloud} installs its public half on the VM when it is created")
        if str(ctx.params["ssh_key_id"] or "").strip():
            ctx.log(f"  warning: ssh_key_id is ignored on {cloud}: it has no key list, so the key in {os.path.basename(f)} is installed on the VM when it is created")
        public = ctx.services.get("public_half", keyfiles.public_half)(f)
        fingerprint = providers.fingerprint_of(public)
        ctx.state["ssh_public_key"] = public
        ctx.log(f"  ssh key: {fingerprint} from {os.path.basename(f)}, installed on the VM when {cloud} creates it")
        return {"ssh_key_id": "", "ssh_key_name": "installed at creation", "ssh_key_fingerprint": fingerprint, "ssh_key_verified": True}

    def _find_key(self, ctx: PackContext, prov) -> Dict[str, Any]:
        cloud, f = self._cloud(ctx), self._key_file(ctx)
        if not f:
            raise PackError("no SSH key to log in with. Make one that every cloud shares: labagent compute ssh-key-add --cloud all --name labagent. "
                            "A launch then finds it in your account by its fingerprint, with no ssh_key_id to copy. Without a key of your own the cloud "
                            "installs its default one, whose private half is not on this machine, and the launch could never log in")
        mine = ctx.services.get("private_fingerprint", keyfiles.private_fingerprint)(f)
        key = next((k for k in prov.ssh_keys() if k.fingerprint and k.fingerprint == mine), None)
        if key is None:
            more = "" if f == keyfiles.default_key_file() else f" --file {shlex.quote(f)}"
            raise PackError(f"the key file {os.path.basename(f)} ({mine}) is not in your {cloud} account, so nothing was rented. "
                            f"Add it (free): labagent compute ssh-key-add --cloud {cloud} --name labagent{more}")
        ctx.state["ssh_key_id"] = key.id                  # the host step builds its request from state: it must see the id found here
        ctx.log(f"  ssh key: {key.name} ({key.id}), {key.fingerprint}, found in {cloud} by the fingerprint of {os.path.basename(f)}")
        return {"ssh_key_id": key.id, "ssh_key_name": key.name, "ssh_key_fingerprint": key.fingerprint, "ssh_key_verified": True}

    def _scrub(self, ctx: PackContext, text: str) -> str:
        for t in ctx.mem.get("tokens", {}).values():
            text = text.replace(t, "***")
        return text

    def _failure(self, ctx: PackContext, what: str, out: str, err: str) -> PackError:
        lines = [l for l in (err or "").splitlines() if l.startswith("ERROR:")] or (err or out or "").strip().splitlines()[-3:]
        return PackError(self._scrub(ctx, f"{what} failed on the VM: {' | '.join(lines)[:400]}"))

    def _remote(self, ctx: PackContext, runner, command: str, what: str, stdin: Optional[str] = None, timeout: int = 900) -> str:
        rc, out, err = runner.run(command, stdin=stdin, timeout=timeout)
        if rc != 0:
            raise self._failure(ctx, what, out, err)
        return out

    def _partition(self, ctx: PackContext, runner) -> str:
        """Split the GPUs. An A100 can need a reboot before MIG turns on: do that once, wait for the VM, and try again."""
        what, cmd = "splitting the GPUs into MIG slices", f"sudo {SCRIPT} partition"
        rc, out, err = runner.run(cmd, timeout=600)
        if rc == REBOOT_RC and "NEEDS_REBOOT=1" in out:
            ctx.log("  MIG takes effect only after a reboot on this GPU: rebooting the VM once (about 2 minutes)")
            self._reboot(ctx, runner)
            rc, out, err = runner.run(cmd, timeout=600)
            if rc == REBOOT_RC:
                raise PackError("MIG did not turn on after a reboot: this GPU or VM does not allow it. Destroy it and try another offer")
        if rc != 0:
            raise self._failure(ctx, what, out, err)
        return out

    def _reboot(self, ctx: PackContext, runner) -> None:
        sleep = ctx.services.get("sleep", time.sleep)
        clock = ctx.services.get("clock", time.monotonic)
        probe = "cat /proc/sys/kernel/random/boot_id"
        rc, before, _ = runner.run(probe, timeout=60)
        if rc != 0 or not before.strip():
            raise PackError("cannot read the VM's boot id, so a reboot could not be confirmed")
        rc, _, err = runner.run("sudo sh -c 'nohup sh -c \"sleep 2; systemctl reboot\" >/dev/null 2>&1 &'", timeout=60)
        if rc != 0:
            raise PackError(f"cannot reboot the VM: {(err or '').strip()[:200]}")
        deadline = clock() + REBOOT_WAIT_S
        while True:
            sleep(POLL_S)
            rc, now, _ = runner.run(probe, timeout=60)
            if rc == 0 and now.strip() and now.strip() != before.strip():
                return
            if clock() > deadline:
                raise PackError(f"the VM did not come back within {REBOOT_WAIT_S // 60} minutes after the reboot")

    # ---- launch ----
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        shared = ctx.params.get("mode") == "shared"
        return [Step("preflight", "Check the offer (read only, nothing is created)", self._preflight, check=lambda c: bool(c.state.get("host_id"))),
                Step("host", "Create the GPU VM and wait until it is active", self._host, check=self._host_ready),
                Step("setup", "Install the lab installer on the VM (no MIG: the labs will share the GPU)" if shared
                     else "Install the lab installer on the VM and split the GPUs into MIG slices", self._setup),
                Step("access", "Set the host name and create the shared login user", self._access),
                Step("labs", "Start the JupyterLab containers, and the HTTPS front door" if shared
                     else "Start one JupyterLab container per slice, and the HTTPS front door", self._labs),
                Step("verify", "Check every lab: it answers, needs its token, and sees the GPU" if shared
                     else "Check every lab: it answers, needs its token, and sees only its own slice", self._verify),
                Step("report", "Print the URL and login of every lab", self._report)]

    def _best(self, ctx: PackContext, prov, cloud: str):
        """No vendor and region given: the cheapest machine of this shape that is available now, in the wanted area."""
        p = ctx.params
        area = p["location"]
        ceiling = float(p["max_price"])
        found = [o for o in prov.offers(providers.OfferQuery(instance_type=str(p["instance_type"]), available_only=True,
                                                             area=None if area == "any" else area))
                 if (not self.skip_containers or o.kind != "container") and (not ceiling or o.hourly_usd <= ceiling)]      # a container has no Docker or MIG (gpu-notebooks-container takes them)
        if not found:
            where = f" in {providers.AREAS[area][0]}" if area in providers.AREAS else ""
            under = f" at ${ceiling:.2f}/h or less" if ceiling else ""
            raise PackError(f"no {p['instance_type']} is available{where}{under} right now. Capacity changes within minutes: try again, or choose another "
                            f"location or GPU. To see what exists: labagent compute offers --cloud {cloud} --type {p['instance_type']}")
        ctx.log(f"  {cloud}: the cheapest of {len(found)} available is {found[0].vendor} in {found[0].region} at ${found[0].hourly_usd:.2f}/h")
        return found[0]                                                   # offers come cheapest first

    def _pick(self, ctx: PackContext, prov) -> Dict[str, str]:
        best = self._best(ctx, prov, self._cloud(ctx))
        return {"vendor": best.vendor, "region": best.region, "os": ctx.params["os"] or providers.best_os(best.os_options) or ""}

    def _auto_clouds(self):
        """The clouds cloud=auto asks, and destroy searches when it does not know the cloud."""
        return sorted(c for c, k in providers.PROVIDERS.items() if not getattr(k, "container_only", False))      # a VM pack never asks a container-only cloud

    def _auto(self, ctx: PackContext):
        """cloud=auto: the cheapest machine across every cloud that has a key here. A cloud that has none of this shape, no capacity, no key, or not
        our SSH key is skipped, with the reason in the log. Returns (provider, key facts, picked), and the chosen cloud is saved in the state."""
        p = ctx.params
        if p["vendor"] or p["region"]:
            raise PackError("cloud=auto picks the cloud, vendor and region itself: leave vendor and region out (location narrows the choice)")
        ranked, skipped = [], []
        for cid in self._auto_clouds():
            try:
                prov = ctx.services["provider"](cid)
                ranked.append((self._best(ctx, prov, cid), cid, prov))
            except Exception as e:                                        # noqa: BLE001  (a cloud that fails must not stop the others)
                skipped.append(f"{cid}: {str(e).splitlines()[0][:160]}")
                ctx.log(f"  auto: skipped {skipped[-1]}")
        ranked.sort(key=lambda r: (r[0].hourly_usd, r[1]))
        for best, cid, prov in ranked:
            ctx.state["cloud"] = cid                                      # the key check and the messages read the cloud from here
            try:
                key_facts = self._check_ssh_key(ctx, prov)
            except PackError as e:
                skipped.append(f"{cid}: {str(e).splitlines()[0][:160]}")
                ctx.log(f"  auto: skipped {skipped[-1]}")
                continue
            picked = {"vendor": best.vendor, "region": best.region, "os": p["os"] or providers.best_os(best.os_options) or ""}
            ctx.log("  auto: " + ", ".join(f"{c} ${b.hourly_usd:.2f}/h" for b, c, _ in ranked) + f" -> {cid}")
            for w in getattr(prov, "warnings", []):
                ctx.log(f"  warning: {w}")
            return prov, key_facts, picked
        ctx.state.pop("cloud", None)
        raise PackError("cloud=auto found no cloud to rent " + f"{p['instance_type']} from. " + "; ".join(skipped or ["no cloud has a key here"]))

    def _preflight(self, ctx: PackContext):
        self._spec(ctx)                                                   # hours are checked first, as before
        self._check_lab_params(ctx)
        picked: Dict[str, str] = {}
        if ctx.params["cloud"] == "auto":
            prov, key_facts, picked = self._auto(ctx)
            ctx.state.update(picked)
        else:
            prov = self._provider(ctx)
            key_facts = self._check_ssh_key(ctx, prov)
            if not (ctx.params["vendor"] and ctx.params["region"]) and ctx.params["location"]:
                picked = self._pick(ctx, prov)
                ctx.state.update(picked)
        spec = self._spec(ctx)
        info = prov.validate(spec)
        if ctx.params.get("mode") == "mig" and info.get("gpu_type") in NO_MIG:
            raise PackError(f"{info['gpu_type']} has no MIG, so it cannot be split into isolated slices. To put several labs on it anyway, "
                            "with no isolation between them, set mode=shared and labs=N")
        ending = (f"deleted by the cloud after {spec.hours} h" if prov.auto_delete else
                  f"NOT deleted by {self._cloud(ctx)} (it has no such setting): it bills until you run `labagent destroy {self.id} --name {ctx.name}`")
        ctx.log(f"  {self._cloud(ctx)}: {spec.instance_type} from {spec.vendor or self._cloud(ctx)} in {spec.region} at ${info['hourly_cents'] / 100:.2f}/h, "
                + ending)
        if ctx.params.get("mode") == "shared":
            ctx.log(f"  warning: shared mode. {ctx.params['labs']} labs will share each GPU's memory with NO isolation: one lab can use it all "
                    "and make the others fail with out-of-memory errors")
        return {"cloud": self._cloud(ctx), "host_name": spec.name, "hourly_cents": info["hourly_cents"], "auto_delete": bool(prov.auto_delete),
                **key_facts, **picked}

    def _host_ready(self, ctx: PackContext) -> bool:
        hid = ctx.state.get("host_id")
        if not hid:
            return False
        h = self._provider(ctx).get(hid)
        return bool(h and h.status == "active" and (h.ip or h.url) and ctx.state.get("ip") == h.ip)    # done only once the address is saved too

    def _host(self, ctx: PackContext):
        prov = self._provider(ctx)
        spec = self._spec(ctx)
        h = prov.get(ctx.state["host_id"]) if ctx.state.get("host_id") else None
        expires = ctx.state.get("expires_utc")
        if h is None or h.status in GONE:
            h = prov.find(spec.name, host_tag(ctx.name))        # a previous run may have created it and died before saving
            if h is None:
                hid = prov.create(spec)
                expires = (datetime.now(timezone.utc) + timedelta(hours=spec.hours)).strftime("%Y-%m-%d %H:%M UTC")
                ctx.state["host_id"] = hid
                ctx.state["expires_utc"] = expires
                save_state(ctx.pack_id, ctx.name, ctx.state)     # now, not after the wait: a timeout must not lose the id of a billable VM
                h = Host(id=hid, name=spec.name)
                ctx.log(f"  created {hid}")
            else:
                ctx.log(f"  adopting the existing VM {h.id}")
        h = self._wait_active(ctx, prov, h.id)
        return {"host_id": h.id, "ip": h.ip, "ssh_user": h.ssh_user, "ssh_port": h.ssh_port, "expires_utc": expires}

    def _elsewhere(self, ctx: PackContext, prov) -> str:
        """After a VM went to an error state at one datacenter, say which one, and name other available offers: the same launch would pick the same cheapest one again."""
        st, p = ctx.state, ctx.params
        vendor, region = p["vendor"] or st.get("vendor"), p["region"] or st.get("region")
        if not vendor and not region:
            return ""
        try:
            others = [o for o in prov.offers(providers.OfferQuery(instance_type=str(p["instance_type"]), available_only=True))
                      if (o.vendor, o.region) != (vendor, region) and o.kind != "container"][:2]
        except Exception:                                                   # a hint must never hide the real error
            others = []
        text = f" at {vendor or self._cloud(ctx)} in {region}: that datacenter failed this machine, which is on its side, not yours"
        if others:
            text += ". Try another: " + "; ".join(f"--param vendor={o.vendor} --param region={o.region} (${o.hourly_usd:.2f}/h)" for o in others)
        return text

    def _wait_active(self, ctx: PackContext, prov, host_id: str) -> Host:
        sleep = ctx.services.get("sleep", time.sleep)
        clock = ctx.services.get("clock", time.monotonic)
        deadline = clock() + WAIT_S
        while True:
            h = prov.get(host_id)
            if h is None or h.status in GONE:
                raise PackError("the VM was deleted while starting (the cloud failed it" + (", or `hours` ran out)" if ctx.state.get("auto_delete", True) else ")"))
            if any(w in h.status for w in ("error", "fail")):
                raise PackError(f"the cloud reports the VM as '{h.status}'" + self._elsewhere(ctx, prov) + ". The VM was deleted so it stops billing")
            if h.status == "active" and (h.ip or h.url):          # a container with only a web address (a Devlab) has no ip
                return h
            if clock() > deadline:
                raise PackError("the VM is not active yet after 20 minutes. Run launch again to keep waiting, or destroy to release it")
            sleep(POLL_S)

    def _setup(self, ctx: PackContext):
        wanted = self._check_lab_params(ctx)
        p = ctx.params
        runner = self._runner(ctx)
        ctx.mem.setdefault("tokens", {})
        sleep = ctx.services.get("sleep", time.sleep)
        clock = ctx.services.get("clock", time.monotonic)
        deadline = clock() + WAIT_SSH_S
        while True:                                         # the VM is "active" a little before sshd answers
            rc, _, err = runner.run("true", timeout=60)
            if rc == 0:
                break
            if clock() > deadline:
                raise PackError("cannot log in to the VM over ssh. Is ssh_key_file the private half of ssh_key_id "
                                f"({ctx.state.get('ssh_key_name') or p['ssh_key_id']}, {ctx.state.get('ssh_key_fingerprint') or 'fingerprint unknown'})? "
                                f"Last error: {(err or '').strip().splitlines()[-1:] or ['none']}")
            sleep(POLL_S)
        home = self._remote(ctx, runner, "echo $HOME", "reading the home directory").strip()
        domain = p["domain"] or default_domain(ctx.state["ip"])
        hostname = p["hostname"] or default_hostname(ctx.name)
        env = "".join(f"{k}='{v}'\n" for k, v in (("MODE", p["mode"]), ("PROFILE", p["mig_profile"]), ("IMAGE", p["image"]), ("TLS", p["tls"]), ("DOMAIN", domain),
                                                   ("LABS_DIR", f"{home}/labs"), ("LAB_HOSTNAME", hostname), ("LOGIN_USER", p["login_user"] or ""),
                                                   ("LOGIN_SUDO", "yes" if flag(p["login_sudo"]) else "no")))
        self._remote(ctx, runner, f"sudo install -d -m 755 {INSTALL_DIR} && sudo tee {INSTALL_DIR}/labs.env >/dev/null", "writing the settings", stdin=env)
        self._remote(ctx, runner, f"sudo tee {SCRIPT} >/dev/null && sudo chmod 755 {SCRIPT}", "installing the lab installer", stdin=setup_script())
        out = self._partition(ctx, runner)
        facts = dict(l.split("=", 1) for l in out.splitlines() if re.fullmatch(r"[A-Z_]+=.*", l))
        slices = int(facts.get("SLICES", "0"))
        if p["mode"] == "shared":
            count = wanted
            ctx.log(f"  {count} labs sharing {facts.get('GPU_COUNT', '?')} x {facts.get('GPU_NAME', 'GPU')}, no isolation between them")
        else:
            if slices < 1:
                raise PackError("the VM reports no MIG slices")
            count = min(slices, wanted or MAX_LABS, MAX_LABS)
            ctx.log(f"  {slices} slices of {facts.get('PROFILE')} on {facts.get('GPU_COUNT', '?')} x {facts.get('GPU_NAME', 'GPU')}: starting {count} labs")
        return {"slices": slices, "lab_count": count, "gpu": facts.get("GPU_NAME", ""), "mode": p["mode"],
                "profile": facts.get("PROFILE", p["mig_profile"]),
                "domain": domain, "remote_home": home, "hostname": hostname}

    def _access(self, ctx: PackContext):
        """Host name, and the user you hand to students. The password travels on stdin and is kept in a 0600 file here."""
        st, p = ctx.state, ctx.params
        if p["login_user"] and p["login_user"] == (st.get("ssh_user") or "shadeform"):
            raise PackError("login_user must differ from the cloud's own login user")
        pw = ""
        f = login_path(ctx.name)
        if p["login_user"]:
            if f.is_file():
                rows = list(csv.DictReader(f.open(encoding="utf-8")))
                pw = rows[0]["password"] if rows and rows[0].get("user") == p["login_user"] else ""
            pw = pw or "".join(secrets.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(20))
            ctx.mem["tokens"]["__login__"] = pw
        else:
            pw = "x" * 16                                    # the script wants a password line even when it only sets the host name
        runner = self._runner(ctx)
        out = self._remote(ctx, runner, f"sudo {SCRIPT} access", "setting the host name and the shared login", stdin=pw + "\n")
        if p["login_user"] and "PROMPT=set" not in out:
            raise PackError("the VM did not confirm the shell prompt of the shared login")
        if p["login_user"]:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch(mode=0o600)
            f.chmod(0o600)
            with f.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["user", "password", "host", "ssh_command", "sudo"])
                w.writerow([p["login_user"], pw, st["domain"], f"ssh {p['login_user']}@{st['domain']}", "yes" if flag(p["login_sudo"]) else "no"])
        return {"login_user": p["login_user"] or "", "login_file": str(f) if p["login_user"] else ""}

    def _tokens(self, ctx: PackContext, names: List[str]) -> Dict[str, str]:
        """Reuse the tokens of an earlier run of this launch, so a rerun does not log learners out."""
        have: Dict[str, str] = {}
        f = credentials_path(ctx.name)
        if f.is_file():
            for row in csv.DictReader(f.open(encoding="utf-8")):
                have[row["lab"]] = row["token"]
        return {n: have.get(n) or secrets.token_hex(16) for n in names}

    def _labs(self, ctx: PackContext):
        st, p = ctx.state, ctx.params
        names = [lab_name(i) for i in range(1, int(st["lab_count"]) + 1)]
        tokens = self._tokens(ctx, names)
        ctx.mem.setdefault("tokens", {}).update(tokens)
        ctx.mem["passwords"] = dict(tokens)         # a job with a public key gets them back sealed, one per lab: the console shows each learner theirs
        base = ("http://" if p["tls"] == "none" else "https://") + st["domain"]
        f = credentials_path(ctx.name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch(mode=0o600)
        f.chmod(0o600)
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["lab", "url", "token", "login_url"])
            for i, n in enumerate(names, 1):
                w.writerow([n, f"{base}/{n}/lab", tokens[n], f"{base}/{n}/lab?token={tokens[n]}"])
        remote_csv = "lab,port,token\n" + "".join(f"{n},{lab_port(i)},{tokens[n]}\n" for i, n in enumerate(names, 1))
        runner = self._runner(ctx)
        d = f"{st['remote_home']}/labs"
        self._remote(ctx, runner, f"umask 077 && mkdir -p {d} && cat > {d}/credentials.csv", "saving the lab logins", stdin=remote_csv)
        self._remote(ctx, runner, f"sudo {SCRIPT} labs", "starting the lab containers", timeout=1800)
        self._remote(ctx, runner, f"sudo {SCRIPT} proxy", "starting the HTTPS front door", timeout=600)
        self._remote(ctx, runner, f"sudo {SCRIPT} install_service", "installing the boot service")
        return {"labs": names, "base_url": base, "credentials_file": str(f)}

    def _verify(self, ctx: PackContext):
        st = ctx.state
        runner = self._runner(ctx)
        out = self._remote(ctx, runner, f"sudo {SCRIPT} verify", "checking the labs", timeout=900)
        seen = {}
        for line in out.splitlines():
            m = re.match(r"LAB (\S+) up=(\d+) anon=(\d+) token=(\d+) \| (.*)", line)
            if m:
                seen[m.group(1)] = m.groups()
        bad: List[str] = []
        for n in st["labs"]:
            row = seen.get(n)
            if not row:
                bad.append(f"{n}: no answer")
                continue
            _, up, anon, tok, gpu = row
            if up != "200":
                bad.append(f"{n}: not up (HTTP {up})")
            elif anon == "200":
                bad.append(f"{n}: answers WITHOUT its token")
            elif tok != "200":
                bad.append(f"{n}: its own token is refused (HTTP {tok})")
            elif not gpu.startswith("1 "):
                bad.append(f"{n}: sees '{gpu[:60]}', expected exactly one GPU" + (" slice" if st.get("mode") != "shared" else ""))
        if bad:
            raise PackError("; ".join(bad)[:500] + ". On the VM: sudo docker logs <lab>")
        sees = "the GPU they share" if st.get("mode") == "shared" else "one slice each"
        ctx.log(f"  all {len(st['labs'])} labs answer, need their token, and see {sees}")
        return {"public_ok": self._public(ctx)}

    def _public(self, ctx: PackContext) -> bool:
        """From this machine, can a learner reach the labs? A failure is a warning: the labs still work through a tunnel."""
        st = ctx.state
        get = ctx.services.get("http_get", _http_status)
        sleep = ctx.services.get("sleep", time.sleep)
        clock = ctx.services.get("clock", time.monotonic)
        verify = ctx.params["tls"] == "letsencrypt"
        urls = [f"{st['base_url']}/{n}/api" for n in st["labs"]]
        deadline = clock() + WAIT_PUBLIC_S
        while get(urls[0], verify) != 200:
            if clock() > deadline:
                ctx.log(f"  warning: {urls[0]} is not reachable from here. Ports 80 and 443 may be closed by the cloud's firewall, "
                        "or the certificate is still being issued. The labs work through the ssh tunnel below.")
                return False
            sleep(POLL_S)
        down = [u for u in urls[1:] if get(u, verify) != 200]
        if down:
            ctx.log(f"  warning: {len(down)} lab URL(s) are not reachable from here, for example {down[0]}")
            return False
        return True

    def _notes(self, ctx: PackContext, rows: List[Dict[str, str]]) -> List[str]:
        """Things the person running this must not miss, in the order they are shown."""
        st, p = ctx.state, ctx.params
        notes = []
        if st.get("login_user") and flag(p["login_sudo"]):
            notes.append("This user has sudo and docker: that is root. Anyone with the password can read every lab's token and stop other labs.")
        if p["tls"] == "internal":
            notes.append("The certificate is self-signed: the browser shows a warning once. Choose to continue.")
        if not st.get("public_ok"):
            fw = " ".join(f"-L {lab_port(i)}:localhost:{lab_port(i)}" for i in range(1, len(rows) + 1))
            notes.append(f"Not reachable from the internet. Through a tunnel instead: ssh -N {fw} {st.get('ssh_user') or 'shadeform'}@{st['ip']}")
            notes.append("then open http://localhost:<port>/<lab>/lab?token=<token> (lab-01 is port 8801, lab-02 is 8802, ...)")
        return notes

    def _report(self, ctx: PackContext):
        st, p = ctx.state, ctx.params
        rows = list(csv.DictReader(credentials_path(ctx.name).open(encoding="utf-8")))
        pw = next(csv.DictReader(login_path(ctx.name).open(encoding="utf-8")))["password"] if st.get("login_user") else None
        if p["show_passwords"] == "no":
            for r in rows:
                r["login_url"] = r["url"]                                 # the link without its token: the token is the login
            pw = "(not shown)" if pw is not None else None
        if ctx.ui:
            self._report_card(ctx, rows, pw)
            return {"ready": True}
        ctx.log("")
        if st.get("mode") == "shared":
            ctx.log(f"GPU labs ready: {len(rows)} x JupyterLab sharing {st.get('gpu') or 'the GPU'}. NO isolation: every lab can use all of the GPU's memory")
        else:
            ctx.log(f"GPU labs ready: {len(rows)} x JupyterLab, one Docker container per {st['profile']} slice of {st.get('gpu') or 'the GPU'}")
        when = f"at about {st['expires_utc']}" if st.get("expires_utc") else f"after the {p['hours']} hours you set"
        if st.get("auto_delete", True):
            ctx.log(f"VM {st['ip']} ({self._cloud(ctx)}), deleted automatically {when}. Stop it sooner: labagent destroy {self.id} --name {ctx.name}")
        else:
            ctx.log(f"VM {st['ip']} ({self._cloud(ctx)}) is NOT deleted automatically: it bills until you run: labagent destroy {self.id} --name {ctx.name}"
                    f"  (planned end {when})")
        ctx.log("Each learner opens their own link. The token in it is the login." if p["show_passwords"] == "yes"
                else "Each learner has a token, kept out of this report. It is in the credentials file below.")
        ctx.log("")
        for r in rows:
            ctx.log(f"  {r['lab']}  {r['login_url']}")
        ctx.log("")
        ctx.log(f"Same list, as a file: {st['credentials_file']}")
        notes = self._notes(ctx, rows)
        if pw is not None:
            ctx.log("")
            ctx.log(f"Shell login to share: ssh {st['login_user']}@{st['domain']}   password: {pw}   (host name {st.get('hostname')}, file {st['login_file']})")
        for note in notes:
            ctx.log(note)
        return {"ready": True}              # from here a failed rerun must not delete a lab that works

    def _report_card(self, ctx: PackContext, rows: List[Dict[str, str]], pw: Optional[str]) -> None:
        """The same facts as the plain report, laid out for a person at a terminal. Strip the colour and nothing is missing."""
        ui, s, st, p = ctx.ui, ctx.ui.s, ctx.state, ctx.params
        shared = st.get("mode") == "shared"
        times = ui.sym["times"]
        gpu = st.get("gpu") or "the GPU"
        what = f"sharing {gpu}" if shared else f"one per {st['profile']} slice of {gpu}"
        price = f"  {ui.sym['dot']}  ${st['hourly_cents'] / 100:.2f}/h" if st.get("hourly_cents") else ""
        when = st.get("expires_utc") or f"in {p['hours']} h"
        lines = [s(f"{len(rows)} {times} JupyterLab", "bold") + "  " + what, ""]
        if shared:
            lines += [s(f"{ui.sym['warn']} NO isolation: every lab can use all of the GPU's memory", "red", "bold"), ""]
        lines += [ui.kv("VM", f"{st['ip']}  {ui.sym['dot']}  {self._cloud(ctx)}{price}"),
                  ui.kv("Expires", f"{when}  " + s("(the cloud deletes the VM)", "gray")) if st.get("auto_delete", True)
                  else ui.kv("Billing", s(f"until you destroy it (planned end {when}, not enforced by {self._cloud(ctx)})", "red")),
                  ui.kv("Stop", s(f"labagent destroy {self.id} --name {ctx.name}", "cyan")), "",
                  s("Each learner opens their own link. The token in it is the login." if p["show_passwords"] == "yes"
                    else "Each learner has a token, kept out of this report. It is in the credentials file.", "gray")]
        for r in rows:
            url, sep, token = r["login_url"].partition("?token=")
            lines.append(s(r["lab"], "bold") + "  " + s(url, "cyan", "underline") + s(sep + token, "gray"))
        lines += ["", ui.kv("Lab file", st["credentials_file"])]
        if pw is not None:
            lines += ["", ui.kv("Shell", f"ssh {st['login_user']}@{st['domain']}"), ui.kv("Password", s(pw, "yellow", "bold")),
                      ui.kv("Host name", str(st.get("hostname"))), ui.kv("Login file", st["login_file"])]
        notes = self._notes(ctx, rows)
        if notes:
            lines.append("")
            lines += [s(ui.sym["warn"], "yellow", "bold") + " " + s(n, "yellow") for n in notes]
        ui.later(lambda: ui.card("GPU labs ready", lines, "green"))

    # ---- destroy ----
    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("host", "Delete the GPU VM this launch created", self._delete)]

    def _delete(self, ctx: PackContext):
        self._release(ctx)
        return {"host_id": None}

    def _release(self, ctx: PackContext) -> Optional[str]:
        """Delete the VM this launch created (by saved id, else by exact name and our tag) and the lab files that belonged to it. Returns the id.
        With cloud=auto and no saved cloud (a new machine, a lost state), look for the VM by name in every cloud that has a key."""
        hid = ctx.state.get("host_id")
        if self._cloud(ctx) == "auto":
            hid = self._delete_anywhere(ctx)
        else:
            prov = self._provider(ctx)
            if not hid:                                          # launch died before saving: find it by exact name and our tag
                h = prov.find(host_name(ctx.name), host_tag(ctx.name))
                hid = h.id if h else None
            if not hid:
                ctx.log("  no VM to delete")
            else:
                prov.delete(hid)
                ctx.log(f"  deleted {hid} (billing stops once the cloud shows it as deleting)")
        if hid:
            _known_hosts(ctx.name, hid).unlink(missing_ok=True)
        credentials_path(ctx.name).unlink(missing_ok=True)      # those tokens belong to a VM that no longer exists
        login_path(ctx.name).unlink(missing_ok=True)
        return hid

    def _delete_anywhere(self, ctx: PackContext) -> Optional[str]:
        """The cloud is not known: it is the one that lists a VM with this exact name and our tag. Only that VM is deleted."""
        for cid in self._auto_clouds():
            try:
                prov = ctx.services["provider"](cid)
                h = prov.find(host_name(ctx.name), host_tag(ctx.name))
            except Exception:                                    # noqa: BLE001  (a cloud with no key here, or down, cannot hold our VM)
                continue
            if h:
                prov.delete(h.id)
                ctx.log(f"  found it in {cid}: deleted {h.id} (billing stops once the cloud shows it as deleting)")
                return h.id
        ctx.log("  no VM to delete: none of the clouds with a key here has one with this name")
        return None

    def on_failure(self, ctx: PackContext, failed_step: str) -> Optional[Dict[str, Any]]:
        """A launch that fails deletes the VM it rented: a GPU left running bills by the hour and serves nobody. Never raises."""
        st = ctx.state
        hid = st.get("host_id")
        if failed_step == "preflight" and not hid:
            return {"action": "nothing", "detail": "nothing was rented"}           # the check stops before anything exists
        if st.get("ready"):
            return {"action": "kept", "host_id": hid, "detail": "this lab was working before this run, so it was left running. "
                    f"Stop it: labagent destroy {self.id} --name {ctx.name}"}
        if flag(ctx.params["keep_on_failure"]):
            ctx.log("  keep_on_failure is on: the VM is still running and billing")
            return {"action": "kept", "host_id": hid, "detail": f"keep_on_failure is on. Stop it: labagent destroy {self.id} --name {ctx.name}"}
        ctx.log("  the launch failed: deleting the GPU VM so it stops billing")
        try:
            gone = self._release(ctx)
        except PackError as e:
            return {"action": "failed", "host_id": hid, "detail": f"could not delete the VM ({str(e)[:200]}). Delete it in the cloud's console "
                    f"or run: labagent destroy {self.id} --name {ctx.name}"}
        return {"action": "deleted" if gone else "nothing", "host_id": gone or hid}
