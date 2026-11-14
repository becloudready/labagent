"""gpu-notebooks: one JupyterLab per slice of a rented GPU (NVIDIA MIG), each in its own Docker container.

`launch` rents a GPU VM, splits every GPU into MIG slices, starts one container per slice with its own JupyterLab and random
token, puts an HTTPS front door in front, checks every lab, and prints a URL and login for each. The VM deletes itself after
`hours`, so a forgotten test cannot bill for days. `destroy` deletes only the VM this launch created.

Where things live: the VM gets /opt/labagent/start-labs.sh (setup.sh) and a systemd unit that rebuilds the slices and
containers at boot. The tokens are written to a 0600 credentials file on this machine and on the VM, and never to state or logs
except the final report that prints them for you.
"""
from __future__ import annotations

import csv
import re
import secrets
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Pack, PackContext, PackError, Param, Step, save_state, state_dir
from . import providers
from .providers import GONE, Host, HostSpec
from .remote import SshRunner

PACK_ID = "gpu-notebooks"
WAIT_S = 1200            # first boot of a GPU VM, including provider-side provisioning
WAIT_SSH_S = 300
WAIT_PUBLIC_S = 240      # certificate issuance for the HTTPS name
POLL_S = 10
MAX_LABS = 99            # lab names are lab-01 .. lab-99
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
    platforms = ("shadeform",)
    cost_tier = "metered"
    default_name = "gpu"
    params = [
        Param("cloud", "shadeform", f"Whose API to call: {', '.join(sorted(providers.PROVIDERS))}"),
        Param("instance_type", "H100", "The cloud's GPU shape, e.g. H100 (one GPU) or H100x8 (eight)"),
        Param("vendor", None, "shadeform: the datacenter company behind the offer, e.g. scaleway. Leave out to list what is available"),
        Param("region", None, "shadeform: that vendor's region, e.g. paris-france-1"),
        Param("os", None, "VM image from the cloud's list, e.g. ubuntu24.04_cuda13.0_shade_os (default: the cloud's)"),
        Param("ssh_key_id", None, "shadeform: id of an SSH key already added to your account (default: your account's default key)"),
        Param("ssh_key_file", None, "Private key file that matches ssh_key_id (default: whatever ssh finds, e.g. ~/.ssh/id_ed25519)"),
        Param("hours", 4, "The cloud deletes the VM after this many hours (1 to 72), whatever happens to this tool"),
        Param("profile", "1g.10gb", "MIG slice size: 1g.10gb (7 per H100), 1g.20gb (4), 2g.20gb (3), 3g.40gb (2)"),
        Param("labs", 0, "How many labs to start (0 = one per slice, at most 99)"),
        Param("image", "quay.io/jupyter/pytorch-notebook:cuda12-latest", "Docker image for each lab. One container per slice"),
        Param("tls", "letsencrypt", "letsencrypt (real certificate on <ip>.sslip.io) | internal (self-signed, browser warning) | none (plain http)"),
        Param("domain", None, "Name the labs are served on (default: <ip>.sslip.io). Its DNS must point at the VM"),
        Param("hostname", None, "Host name of the VM, shown in every shell prompt (default: the launch --name, e.g. nvidia-h100-hackathon)"),
        Param("login_user", "labagent", "A user to share with students: ssh with a password. Empty = do not create one"),
        Param("login_sudo", True, "Give login_user sudo and docker. WARNING: that is root, so one student can read every lab's token and stop other labs"),
    ]
    output_keys = ("cloud", "host_name", "host_id", "ip", "ssh_user", "ssh_port", "hourly_cents", "expires_utc", "gpu", "profile", "slices",
                   "domain", "base_url", "credentials_file", "public_ok", "hostname", "login_user", "login_file")

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
        return HostSpec(name=host_name(ctx.name), instance_type=str(p["instance_type"]), vendor=p["vendor"], region=p["region"],
                        os_image=p["os"], ssh_key_id=p["ssh_key_id"], hours=hours, tags=["labagent", host_tag(ctx.name)])

    def _check_lab_params(self, ctx: PackContext) -> int:
        p = ctx.params
        if not re.fullmatch(r"[0-9a-z.+]+", str(p["profile"])):
            raise PackError("profile must look like 1g.10gb")
        if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", str(p["image"])):
            raise PackError("image must be a Docker image name such as quay.io/jupyter/pytorch-notebook:cuda12-latest")
        if p["tls"] not in ("letsencrypt", "internal", "none"):
            raise PackError("tls must be letsencrypt, internal or none")
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
        return n

    def _provider(self, ctx: PackContext):
        prov = ctx.services["provider"](ctx.params["cloud"])
        for w in getattr(prov, "warnings", []):
            ctx.log(f"  warning: {w}")
        return prov

    def _runner(self, ctx: PackContext):
        st = ctx.state
        kh = _known_hosts(ctx.name, st["host_id"])
        kh.parent.mkdir(parents=True, exist_ok=True)
        return ctx.services["ssh"](st["ip"], st.get("ssh_user") or "shadeform", st.get("ssh_port") or 22, ctx.params["ssh_key_file"], str(kh))

    def _scrub(self, ctx: PackContext, text: str) -> str:
        for t in ctx.mem.get("tokens", {}).values():
            text = text.replace(t, "***")
        return text

    def _remote(self, ctx: PackContext, runner, command: str, what: str, stdin: Optional[str] = None, timeout: int = 900) -> str:
        rc, out, err = runner.run(command, stdin=stdin, timeout=timeout)
        if rc != 0:
            lines = [l for l in (err or "").splitlines() if l.startswith("ERROR:")] or (err or out or "").strip().splitlines()[-3:]
            raise PackError(self._scrub(ctx, f"{what} failed on the VM: {' | '.join(lines)[:400]}"))
        return out

    # ---- launch ----
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("preflight", "Check the offer (read only, nothing is created)", self._preflight, check=lambda c: bool(c.state.get("host_id"))),
                Step("host", "Create the GPU VM and wait until it is active", self._host, check=self._host_ready),
                Step("setup", "Install the lab installer on the VM and split the GPUs into MIG slices", self._setup),
                Step("access", "Set the host name and create the shared login user", self._access),
                Step("labs", "Start one JupyterLab container per slice, and the HTTPS front door", self._labs),
                Step("verify", "Check every lab: it answers, needs its token, and sees only its own slice", self._verify),
                Step("report", "Print the URL and login of every lab", self._report)]

    def _preflight(self, ctx: PackContext):
        spec = self._spec(ctx)
        self._check_lab_params(ctx)
        info = self._provider(ctx).validate(spec)
        ctx.log(f"  {ctx.params['cloud']}: {spec.instance_type} from {spec.vendor} in {spec.region} at ${info['hourly_cents'] / 100:.2f}/h, "
                f"deleted by the cloud after {spec.hours} h")
        return {"cloud": ctx.params["cloud"], "host_name": spec.name, "hourly_cents": info["hourly_cents"]}

    def _host_ready(self, ctx: PackContext) -> bool:
        hid = ctx.state.get("host_id")
        if not hid:
            return False
        h = self._provider(ctx).get(hid)
        return bool(h and h.status == "active" and h.ip and ctx.state.get("ip") == h.ip)    # done only once the address is saved too

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

    def _wait_active(self, ctx: PackContext, prov, host_id: str) -> Host:
        sleep = ctx.services.get("sleep", time.sleep)
        clock = ctx.services.get("clock", time.monotonic)
        deadline = clock() + WAIT_S
        while True:
            h = prov.get(host_id)
            if h is None or h.status in GONE:
                raise PackError("the VM was deleted while starting (the cloud failed it, or `hours` ran out)")
            if any(w in h.status for w in ("error", "fail")):
                raise PackError(f"the cloud reports the VM as '{h.status}'. Run destroy, then launch again")
            if h.status == "active" and h.ip:
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
                raise PackError("cannot log in to the VM over ssh. Is ssh_key_file the private half of ssh_key_id? "
                                f"Last error: {(err or '').strip().splitlines()[-1:] or ['none']}")
            sleep(POLL_S)
        home = self._remote(ctx, runner, "echo $HOME", "reading the home directory").strip()
        domain = p["domain"] or default_domain(ctx.state["ip"])
        hostname = p["hostname"] or default_hostname(ctx.name)
        env = "".join(f"{k}='{v}'\n" for k, v in (("PROFILE", p["profile"]), ("IMAGE", p["image"]), ("TLS", p["tls"]), ("DOMAIN", domain),
                                                   ("LABS_DIR", f"{home}/labs"), ("LAB_HOSTNAME", hostname), ("LOGIN_USER", p["login_user"] or ""),
                                                   ("LOGIN_SUDO", "yes" if flag(p["login_sudo"]) else "no")))
        self._remote(ctx, runner, f"sudo install -d -m 755 {INSTALL_DIR} && sudo tee {INSTALL_DIR}/labs.env >/dev/null", "writing the settings", stdin=env)
        self._remote(ctx, runner, f"sudo tee {SCRIPT} >/dev/null && sudo chmod 755 {SCRIPT}", "installing the lab installer", stdin=setup_script())
        out = self._remote(ctx, runner, f"sudo {SCRIPT} partition", "splitting the GPUs into MIG slices", timeout=600)
        facts = dict(l.split("=", 1) for l in out.splitlines() if re.fullmatch(r"[A-Z_]+=.*", l))
        slices = int(facts.get("SLICES", "0"))
        if slices < 1:
            raise PackError("the VM reports no MIG slices")
        count = min(slices, wanted or MAX_LABS, MAX_LABS)
        ctx.log(f"  {slices} slices of {facts.get('PROFILE')} on {facts.get('GPU_COUNT', '?')} x {facts.get('GPU_NAME', 'GPU')}: starting {count} labs")
        return {"slices": slices, "lab_count": count, "gpu": facts.get("GPU_NAME", ""), "profile": facts.get("PROFILE", p["profile"]),
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
                bad.append(f"{n}: sees '{gpu[:60]}', expected exactly one GPU slice")
        if bad:
            raise PackError("; ".join(bad)[:500] + ". On the VM: sudo docker logs <lab>")
        ctx.log(f"  all {len(st['labs'])} labs answer, need their token, and see one slice each")
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

    def _report(self, ctx: PackContext):
        st, p = ctx.state, ctx.params
        rows = list(csv.DictReader(credentials_path(ctx.name).open(encoding="utf-8")))
        ctx.log("")
        ctx.log(f"GPU labs ready: {len(rows)} x JupyterLab, one Docker container per {st['profile']} slice of {st.get('gpu') or 'the GPU'}")
        when = f"at about {st['expires_utc']}" if st.get("expires_utc") else f"after the {p['hours']} hours you set"
        ctx.log(f"VM {st['ip']} ({p['cloud']}), deleted automatically {when}. Stop it sooner: labagent destroy {PACK_ID} --name {ctx.name}")
        ctx.log("Each learner opens their own link. The token in it is the login.")
        ctx.log("")
        for r in rows:
            ctx.log(f"  {r['lab']}  {r['login_url']}")
        ctx.log("")
        ctx.log(f"Same list, as a file: {st['credentials_file']}")
        if st.get("login_user"):
            pw = next(csv.DictReader(login_path(ctx.name).open(encoding="utf-8")))["password"]
            ctx.log("")
            ctx.log(f"Shell login to share: ssh {st['login_user']}@{st['domain']}   password: {pw}   (host name {st.get('hostname')}, file {st['login_file']})")
            if flag(p["login_sudo"]):
                ctx.log("This user has sudo and docker: that is root. Anyone with the password can read every lab's token and stop other labs.")
        if p["tls"] == "internal":
            ctx.log("The certificate is self-signed: the browser shows a warning once. Choose to continue.")
        if not st.get("public_ok"):
            fw = " ".join(f"-L {lab_port(i)}:localhost:{lab_port(i)}" for i in range(1, len(rows) + 1))
            ctx.log(f"Not reachable from the internet. Through a tunnel instead: ssh -N {fw} {st.get('ssh_user') or 'shadeform'}@{st['ip']}")
            ctx.log("then open http://localhost:<port>/<lab>/lab?token=<token> (lab-01 is port 8801, lab-02 is 8802, ...)")
        return {}

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
        else:
            prov.delete(hid)
            ctx.log(f"  deleted {hid} (billing stops once the cloud shows it as deleting)")
            _known_hosts(ctx.name, hid).unlink(missing_ok=True)
        credentials_path(ctx.name).unlink(missing_ok=True)      # those tokens belong to a VM that no longer exists
        login_path(ctx.name).unlink(missing_ok=True)
        return {"host_id": None}
