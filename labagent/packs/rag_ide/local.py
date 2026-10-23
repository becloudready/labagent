"""Local platform for rag-ide: the same one-container-per-learner lab, on this machine through Docker. For demos and tests.

It mirrors the EC2 workflow (same pack steps, credentials file and destroy), minus the cloud parts: no HTTPS proxy, each learner
gets http://127.0.0.1:<port>. Every port is bound to loopback only, containers are unprivileged and capped, and a password is
always required. Nothing here talks to a cloud account.
"""
from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from ..base import PackError
from .ec2 import IMAGE
from .modal_gateway import slug

LAB_DIR = "/home/coder/lab"


def container_name(cohort: str, learner: str) -> str:
    return f"labagent-lab-{slug(cohort, 20)}-{slug(learner, 20)}"


def volume_name(cohort: str, learner: str) -> str:
    return f"labagent-lab-{slug(cohort, 20)}-{slug(learner, 20)}-work"


def learner_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def run_args(cohort: str, learner: str, password: str, port: int, *, image: str, cpu: float, memory_mib: int, hours: int,
             idle_s: int, env: Optional[Dict[str, str]] = None) -> List[str]:
    """The docker run command for one learner (a list, never a shell string). The password travels in the environment of the
    docker CLI process (`-e PASSWORD`), not in the argument list, so it does not show up in `ps`."""
    args = ["docker", "run", "-d", "--name", container_name(cohort, learner), "-p", f"127.0.0.1:{port}:8080",
            "--label", "labagent=rag-ide", "--label", f"cohort={slug(cohort)}", "--label", f"learner={slug(learner)}",
            "--cpus", f"{cpu:g}", "--memory", f"{memory_mib}m", "--pids-limit", "1024",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-e", "PASSWORD", "-e", "HF_HUB_OFFLINE=1"]
    for k in (env or {}):
        args += ["-e", k]
    args += ["-v", f"{volume_name(cohort, learner)}:{LAB_DIR}/work", image,
             "sh", "-c", f"exec timeout {hours * 3600} code-server --bind-addr 0.0.0.0:8080 --auth password "
                         f"--idle-timeout-seconds {idle_s} {LAB_DIR}"]
    return args


class LocalDockerGateway:
    def __init__(self) -> None:
        self.docker = "docker"

    def _run(self, args: List[str], env: Optional[Dict[str, str]] = None, check: bool = True) -> subprocess.CompletedProcess:
        import os
        r = subprocess.run(args, capture_output=True, text=True, env={**os.environ, **(env or {})})
        if check and r.returncode != 0:
            raise PackError(f"docker failed: {(r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout).strip() else 'unknown error'}")
        return r

    def check_auth(self) -> str:
        try:
            r = self._run([self.docker, "info", "--format", "{{.ServerVersion}}"], check=False)
        except FileNotFoundError as e:
            raise PackError("docker is not installed. Install Docker Desktop, or use platform=ec2 or platform=modal.") from e
        if r.returncode != 0:
            raise PackError("docker is installed but its daemon is not running. Start Docker Desktop and retry.")
        return f"local docker {r.stdout.strip()}"

    def ensure_image(self, image: str, source: str) -> None:
        if self._run([self.docker, "image", "inspect", image], check=False).returncode == 0:
            return
        dockerfile = Path(source) / "Dockerfile.lab"
        if not dockerfile.is_file():
            raise PackError(f"image '{image}' not found and there is no Dockerfile.lab to build it from. "
                            "Run from a labagent checkout, or pass image=<an existing image tag>.")
        r = subprocess.run([self.docker, "build", "-f", str(dockerfile), "-t", image, str(dockerfile.parent)])
        if r.returncode != 0:
            raise PackError("docker build failed")

    def launch(self, cohort: str, learner: str, password: str, port: int, **kw) -> Dict[str, str]:
        vol = volume_name(cohort, learner)
        image = kw["image"]
        self._run([self.docker, "volume", "create", vol])
        self._run([self.docker, "run", "--rm", "-u", "root", "-v", f"{vol}:/w", image, "chown", "-R", "1000:1000", "/w"])
        self._run(run_args(cohort, learner, password, port, **kw), env={"PASSWORD": password, **(kw.get("env") or {})})
        return {"container": container_name(cohort, learner), "url": learner_url(port), "volume": vol, "port": str(port)}

    def ready(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url + "login", timeout=5) as r:
                return r.status == 200
        except Exception:
            return False

    def alive(self, container: str) -> bool:
        r = self._run([self.docker, "inspect", "-f", "{{.State.Running}}", container], check=False)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def find(self, cohort: str) -> List[str]:
        r = self._run([self.docker, "ps", "-a", "--filter", "label=labagent=rag-ide", "--filter", f"label=cohort={slug(cohort)}", "--format", "{{.Names}}"], check=False)
        return [n for n in r.stdout.split() if n]

    def remove(self, container: str) -> None:
        self._run([self.docker, "rm", "-f", container], check=False)

    def delete_volume(self, name: str) -> None:
        self._run([self.docker, "volume", "rm", name], check=False)

    def pause(self, seconds: float) -> None:
        import time
        time.sleep(seconds)
