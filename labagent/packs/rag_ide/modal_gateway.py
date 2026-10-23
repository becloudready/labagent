"""Everything that touches Modal lives here, behind a small interface, so the pack logic is testable with a fake
and a second platform can be added later by writing another gateway with the same methods.

Why Sandboxes (not Functions, as in the first spike): each learner gets their own volume, their own tag, and can be stopped on
their own. The code-server process runs as an unprivileged user, never root.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import PackError

CODE_SERVER_VERSION = "4.141.0"
CODE_SERVER_SHA256_AMD64 = "f384beb4f3ba207e0bca27d0dbd36dd825d5aa909d78c6d4baa8703eaf6b77ba"   # Modal workers are x86_64
MODEL = "all-MiniLM-L6-v2"
LAB_DIR = "/home/coder/lab"
WORK_DIR = LAB_DIR + "/work"       # the learner's persistent volume
PORT = 8080

# Pip spec for the labagent that goes into the image: a checkout path ("local") or the PyPI release that contains labagent.ai.rag.
PYPI_SPEC = "labagent[rag-lab]>=0.3.0"
IGNORE = [".git", ".venv", "venv", "dist", "*.egg-info", "**/__pycache__", "**/.terraform", "tests", "docs", "spikes", ".pytest_cache"]


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:limit] or "x"


def volume_name(cohort: str, learner: str) -> str:
    return f"labagent-rag-ide-{slug(cohort, 20)}-{slug(learner, 20)}"


def app_name(cohort: str) -> str:
    return f"labagent-rag-ide-{slug(cohort, 30)}"


def start_script(idle_seconds: int) -> str:
    """Runs as root only long enough to hand the volume to the learner's user, then replaces itself with code-server as that user."""
    # WORK_DIR is a symlink into the volume: chown the target, not the link.
    return (f"chown -R coder:coder \"$(readlink -f {WORK_DIR})\" 2>/dev/null; "
            f"exec runuser -u coder -- env HOME=/home/coder code-server --bind-addr 0.0.0.0:{PORT} --auth password "
            f"--idle-timeout-seconds {idle_seconds} {LAB_DIR}")


class ModalGateway:
    """Real Modal calls. Imported lazily so labagent works without the `modal` extra."""

    def __init__(self) -> None:
        try:
            import modal
        except ImportError as e:
            raise PackError("the 'modal' package is not installed. Fix: pip install 'labagent[modal]' then run: modal token new") from e
        self.modal = modal

    # -- auth
    def check_auth(self) -> str:
        """Returns a label for the active login, or raises with the one command that fixes it."""
        try:
            from modal.config import config
            if not (config.get("token_id") and config.get("token_secret")):
                raise PackError("no Modal login found. Run: modal token new")
            return os.environ.get("MODAL_PROFILE") or "your Modal account"
        except PackError:
            raise
        except Exception as e:
            raise PackError(f"could not read the Modal login ({type(e).__name__}). Run: modal token new") from e

    # -- image
    def image(self, source: str) -> Any:
        """source is 'pypi' or a path to a labagent checkout. Built once per change and cached by Modal."""
        m = self.modal
        img = (m.Image.debian_slim(python_version="3.11")
               .apt_install("curl", "git", "ca-certificates")
               .run_commands(
                   "set -eux; curl -fsSL -o /tmp/cs.deb https://github.com/coder/code-server/releases/download/"
                   f"v{CODE_SERVER_VERSION}/code-server_{CODE_SERVER_VERSION}_amd64.deb; "
                   f"echo '{CODE_SERVER_SHA256_AMD64}  /tmp/cs.deb' | sha256sum -c -; dpkg -i /tmp/cs.deb; rm /tmp/cs.deb",
                   "useradd -m -s /bin/bash -u 1000 coder")
               .pip_install("torch", index_url="https://download.pytorch.org/whl/cpu"))
        if source == "pypi":
            img = img.pip_install(PYPI_SPEC)
        else:
            img = img.add_local_dir(source, "/src", copy=True, ignore=IGNORE).run_commands('pip install "/src[rag-lab]"')
        return (img
                .env({"HF_HOME": "/opt/hf-cache", "SENTENCE_TRANSFORMERS_HOME": "/opt/hf-cache", "HF_HUB_OFFLINE": "0"})
                .run_commands(f"python -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('{MODEL}')\"", "chmod -R a+rX /opt/hf-cache")
                .env({"HF_HUB_OFFLINE": "1"})
                .run_commands(f"labagent rag init --dir {LAB_DIR}", f"mkdir -p {WORK_DIR}", "chown -R coder:coder /home/coder",
                              "python -m ipykernel install --sys-prefix --name python3",
                              "runuser -u coder -- env HOME=/home/coder code-server --install-extension ms-python.python "
                              "--install-extension ms-toolsai.jupyter || echo 'extensions not installed'"))

    def app(self, name: str) -> Any:
        return self.modal.App.lookup(name, create_if_missing=True)

    def build(self, app: Any, image: Any) -> None:
        image.build(app)

    # -- learners
    def launch(self, app: Any, cohort: str, learner: str, password: str, image: Any, cpu: float, memory_mib: int,
               timeout_s: int, idle_s: int, env: Dict[str, str]) -> Dict[str, str]:
        m = self.modal
        vol = m.Volume.from_name(volume_name(cohort, learner), create_if_missing=True)
        sb = m.Sandbox.create("sh", "-c", start_script(idle_s), app=app, image=image, cpu=cpu, memory=memory_mib, timeout=timeout_s,
                              volumes={WORK_DIR: vol}, encrypted_ports=[PORT], env={"PASSWORD": password, **env})
        sb.set_tags({"labagent": "rag-ide", "cohort": slug(cohort), "learner": slug(learner)})
        tunnel = sb.tunnels(timeout=120)[PORT]
        return {"sandbox_id": sb.object_id, "url": tunnel.url, "volume": volume_name(cohort, learner)}

    def is_alive(self, sandbox_id: str) -> bool:
        try:
            return self.modal.Sandbox.from_id(sandbox_id).poll() is None
        except Exception:
            return False

    def stop(self, sandbox_id: str) -> None:
        try:
            self.modal.Sandbox.from_id(sandbox_id).terminate()
        except Exception:
            pass          # already gone

    def find(self, cohort: str) -> List[str]:
        return [sb.object_id for sb in self.modal.Sandbox.list(tags={"labagent": "rag-ide", "cohort": slug(cohort)})]

    def delete_volume(self, name: str) -> None:
        try:
            self.modal.Volume.delete(name)
        except Exception:
            pass
