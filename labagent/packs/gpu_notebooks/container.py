"""gpu-notebooks-container: a GPU machine on a cloud that rents containers (a RunPod pod), with the cloud's OWN JupyterLab and its own URL.

A container has no Docker and no MIG, and installing a second JupyterLab inside one is fragile (a first attempt did, and its front-end assets were missing).
So this pack installs nothing. It creates the machine with the cloud's Jupyter switched on and a password it chose, waits for the machine, and reports the URL
the cloud gives that Jupyter and the password. One machine, one JupyterLab, one GPU (all of it).

Same launch and destroy as gpu-notebooks, same SSH key handling, and a failed launch still deletes the machine it rented. A cloud that cannot start its own
JupyterLab (`Provider.builtin_jupyter` False) is refused before anything is rented: use gpu-notebooks there.
"""
from __future__ import annotations

import csv
import secrets
import time
from typing import List

from ..base import PackContext, PackError, Param, Step
from .pack import POLL_S, WAIT_PUBLIC_S, GpuNotebooks, credentials_path
from ...compute import providers

PACK_ID = "gpu-notebooks-container"
JUPYTER_PORT = 8888
KEEP = ("cloud", "instance_type", "vendor", "region", "location", "max_price", "os", "ssh_key_id", "ssh_key_file", "keep_on_failure", "hours", "show_passwords")
REACHABLE = (200, 301, 302, 303, 401, 403)          # the page, a redirect to its login, or "log in first": Jupyter answered. 5xx and None: not yet


def _params() -> List[Param]:
    out: List[Param] = []
    for p in GpuNotebooks.params:
        if p.name not in KEEP:
            continue
        if p.name == "cloud":
            out.append(Param("cloud", "runpod", "Whose API to call. Only clouds that start their own JupyterLab on a machine they create: runpod. (auto picks among those)"))
        elif p.name == "os":
            out.append(Param("os", None, "The machine's Docker image. Default runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404 (RunPod's official images start JupyterLab)"))
        else:
            out.append(p)
    return out


class GpuNotebooksContainer(GpuNotebooks):
    id = PACK_ID
    title = "GPU notebooks on containers: the cloud's own JupyterLab"
    summary = "A rented GPU pod with the cloud's own JupyterLab and URL"
    aliases = ("gpu-labs-container", "container-labs")
    platforms = ("runpod",)
    skip_containers = False
    params = _params()
    output_keys = ("cloud", "host_name", "host_id", "vendor", "region", "ip", "ssh_user", "ssh_port", "hourly_cents", "expires_utc", "gpu", "url",
                   "credentials_file", "public_ok", "ssh_key_id", "ssh_key_name", "ssh_key_fingerprint", "ssh_key_verified", "auto_delete")

    def outputs(self, state):
        return {k: state[k] for k in self.output_keys if k in state}               # the URL has no password in it; that is in the credentials file and the report

    # ---- helpers ----
    def _password(self, ctx: PackContext) -> str:
        """Chosen once and kept in the 0600 credentials file, so a rerun does not change the login. Never in state, outputs or logs (except the final report)."""
        f = credentials_path(ctx.name)
        pw = ""
        if f.is_file():
            rows = list(csv.DictReader(f.open(encoding="utf-8")))
            pw = rows[0].get("token", "") if rows else ""
        if not pw:
            pw = secrets.token_urlsafe(18)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch(mode=0o600)
            f.chmod(0o600)
            with f.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["lab", "url", "token", "login_url"])
                w.writerow(["jupyter", "", pw, ""])                                     # the url is added once the pod exists
        ctx.mem["passwords"] = {"jupyter": pw}
        return pw

    def _spec(self, ctx: PackContext):
        spec = super()._spec(ctx)
        spec.jupyter_password = self._password(ctx)
        spec.ports = [JUPYTER_PORT]
        return spec

    def _check_lab_params(self, ctx: PackContext) -> int:
        p = ctx.params
        if p["location"] and p["location"] not in providers.LOCATIONS:
            raise PackError(f"location must be one of: {', '.join(providers.LOCATIONS)}")
        if p["show_passwords"] not in ("yes", "no"):
            raise PackError("show_passwords must be yes or no")
        if str(p["keep_on_failure"]).strip().lower() not in ("yes", "no", "true", "false", "1", "0", "on", "off"):
            raise PackError("keep_on_failure must be yes or no")
        try:
            if float(p["max_price"]) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise PackError("max_price must be a number of dollars per hour, or 0 for no limit")
        cloud = str(p["cloud"])
        if cloud not in ("auto",) + self.platforms:
            raise PackError(f"cloud {cloud} cannot start its own JupyterLab on a machine it creates, so this pack cannot use it. "
                            f"Clouds that can: {', '.join(self.platforms)}. For VMs (lambda, nebius, shadeform) use gpu-notebooks")
        return 1

    def _auto_clouds(self):
        return self.platforms

    # ---- launch ----
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("preflight", "Check the offer (read only, nothing is created)", self._preflight, check=lambda c: bool(c.state.get("host_id"))),
                Step("host", "Create the GPU machine with its own JupyterLab and wait until it is active", self._host, check=self._host_ready),
                Step("url", "Work out the JupyterLab URL and wait until it answers", self._url),
                Step("report", "Print the URL and password", self._report)]

    def _url(self, ctx: PackContext):
        st = ctx.state
        prov = self._provider(ctx)
        base = prov.web_url(st["host_id"], JUPYTER_PORT)
        if not base:
            raise PackError(f"{self._cloud(ctx)} gave no URL for its JupyterLab. Use the ssh tunnel: ssh -N -L {JUPYTER_PORT}:localhost:{JUPYTER_PORT} "
                            f"-p {st.get('ssh_port') or 22} {st.get('ssh_user') or 'root'}@{st['ip']}")
        pw = self._password(ctx)
        f = credentials_path(ctx.name)
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["lab", "url", "token", "login_url"])
            w.writerow(["jupyter", base, pw, f"{base}/?token={pw}"])
        get = ctx.services.get("http_get")
        if get is None:
            from .pack import _http_status as get
        sleep = ctx.services.get("sleep", time.sleep)
        clock = ctx.services.get("clock", time.monotonic)
        deadline = clock() + WAIT_PUBLIC_S
        ok = True
        while get(base + "/", True) not in REACHABLE:                   # JupyterLab takes a minute or two after the pod is active
            if clock() > deadline:
                ctx.log(f"  warning: {base} did not answer from here within {WAIT_PUBLIC_S} s. It may still be starting: try the link in a minute. "
                        f"If it never opens, read the pod's logs in the cloud's console")
                ok = False
                break
            sleep(POLL_S)
        if ok:
            ctx.log(f"  {base} answers")
        return {"url": base, "credentials_file": str(f), "public_ok": ok}

    def _report(self, ctx: PackContext):
        st, p = ctx.state, ctx.params
        row = list(csv.DictReader(credentials_path(ctx.name).open(encoding="utf-8")))[0]
        shown = p["show_passwords"] == "yes"
        ctx.log("")
        ctx.log(f"JupyterLab ready on {st.get('gpu') or 'the GPU'}: the cloud's own JupyterLab, one GPU machine" if st.get("public_ok") else
                "JupyterLab is starting (see the warning above)")
        when = f"at about {st['expires_utc']}" if st.get("expires_utc") else f"after the {p['hours']} hours you set"
        if st.get("auto_delete", True):
            ctx.log(f"Machine {st['ip']} ({self._cloud(ctx)}), deleted automatically {when}. Stop it sooner: labagent destroy {PACK_ID} --name {ctx.name}")
        else:
            ctx.log(f"Machine {st['ip']} ({self._cloud(ctx)}) is NOT deleted automatically: it bills until you run: labagent destroy {PACK_ID} --name {ctx.name}"
                    f"  (planned end {when})")
        ctx.log("")
        ctx.log(f"  URL       {row['url']}")
        if shown:
            ctx.log(f"  Password  {row['token']}   (if it asks for a token instead, use the same value, or open {row['login_url']})")
        else:
            ctx.log(f"  Password  (not shown: it is in {st['credentials_file']})")
        ctx.log("")
        ctx.log(f"Shell: ssh -i <your key> -p {st.get('ssh_port') or 22} {st.get('ssh_user') or 'root'}@{st['ip']}")
        ctx.log(f"Same details as a file: {st['credentials_file']}")
        return {"ready": True}              # from here a failed rerun must not delete a machine that works
