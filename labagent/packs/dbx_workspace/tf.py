"""A small, safe wrapper around the terraform CLI for one workspace.

- The module is copied to a working folder under the pack's state folder, so the packaged files are never changed.
- Credentials are read from the environment by the providers. They never go on a command line, in a var file or in state.
- A plan is saved and then applied as a file, so what runs is exactly what was reviewed.
- Output is cleaned before it is shown: colour codes, secret values and email addresses are removed, and only the tail is kept.
- The subprocess runner is injected, so tests never start terraform or touch a cloud.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..base import PackError

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SECRET_ENV = ("DATABRICKS_CLIENT_SECRET", "DATABRICKS_TOKEN", "ARM_CLIENT_SECRET", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "ARM_ACCESS_KEY")
MODULES = Path(__file__).resolve().parents[2] / "terraform"


def clean(text: str, env: Optional[Dict[str, str]] = None, tail: int = 14) -> str:
    env = env if env is not None else os.environ
    text = ANSI.sub("", text)
    for name in SECRET_ENV:
        v = env.get(name)
        if v and len(v) >= 6:
            text = text.replace(v, "<redacted>")
    lines = [EMAIL.sub("<redacted>", ln) for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-tail:])[:1800]


def summarize_plan(plan_json: Dict[str, Any]) -> Dict[str, Any]:
    """Counts only: no values, so a plan summary is safe to log and to return."""
    out = {"add": 0, "change": 0, "destroy": 0, "replace": 0, "types": {}}
    for rc in plan_json.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions == ["create"]:
            out["add"] += 1
            out["types"][rc["type"]] = out["types"].get(rc["type"], 0) + 1
        elif actions == ["update"]:
            out["change"] += 1
        elif actions == ["delete"]:
            out["destroy"] += 1
            out["types"][rc["type"]] = out["types"].get(rc["type"], 0) + 1
        elif sorted(actions) == ["create", "delete"]:
            out["replace"] += 1
    out["types"] = dict(sorted(out["types"].items()))
    return out


class Tf:
    def __init__(self, workdir: Path, run: Callable[..., Any] = subprocess.run, binary: Optional[str] = None, env: Optional[Dict[str, str]] = None):
        self.dir = Path(workdir)
        self._run = run
        self._binary = binary
        self.env = dict(os.environ if env is None else env)

    def binary(self) -> str:
        b = self._binary or self.env.get("LABAGENT_TF_BIN") or shutil.which("terraform") or shutil.which("tofu")
        if not b:
            raise PackError("terraform was not found. Install Terraform 1.9 or later (or OpenTofu), or set LABAGENT_TF_BIN")
        return b

    def _exec(self, args: List[str], extra_env: Optional[Dict[str, str]] = None) -> str:
        env = {**self.env, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1", **(extra_env or {})}
        env.pop("TF_LOG", None)                            # debug logs print request bodies
        p = self._run([self.binary(), *args], capture_output=True, text=True, env=env, cwd=str(self.dir))
        if p.returncode != 0:
            raise PackError(f"terraform {args[0]} failed (exit {p.returncode}):\n{clean((p.stderr or '') + (p.stdout or ''), env)}")
        return p.stdout or ""

    # ---- the module folder ---------------------------------------------------------------------------------------------------------
    def prepare(self, cloud: str, variables: Dict[str, Any], backend: Dict[str, Any]) -> None:
        """Copy the packaged module, add the backend, and write the (non-secret) variables with mode 0600."""
        src = MODULES / ("databricks_aws" if cloud == "aws" else "databricks_azure")
        if self.dir.exists():
            shutil.rmtree(self.dir)
        shutil.copytree(src, self.dir, ignore=shutil.ignore_patterns(".terraform", "tests", "*.tfstate*", "plan.bin"))
        kind = backend["type"]
        (self.dir / "backend.tf").write_text(f'terraform {{\n  backend "{kind}" {{}}\n}}\n')
        fd = os.open(self.dir / "terraform.tfvars.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(variables, f, indent=2, sort_keys=True)
        self.backend = backend

    def init(self) -> None:
        args = ["init", "-input=false", "-no-color", "-reconfigure", "-lockfile=readonly"]
        args += [f"-backend-config={k}={v}" for k, v in sorted(self.backend["config"].items())]
        self._exec(args)

    def plan(self, destroy: bool = False) -> Dict[str, Any]:
        args = ["plan", "-input=false", "-no-color", "-var-file=terraform.tfvars.json", "-out=plan.bin"] + (["-destroy"] if destroy else [])
        self._exec(args)
        plan = self.dir / "plan.bin"
        if plan.exists():
            plan.chmod(0o600)
        return summarize_plan(json.loads(self._exec(["show", "-json", "-no-color", "plan.bin"])))

    def apply_plan(self) -> None:
        self._exec(["apply", "-input=false", "-no-color", "plan.bin"])
        (self.dir / "plan.bin").unlink(missing_ok=True)

    def outputs(self) -> Dict[str, Any]:
        raw = json.loads(self._exec(["output", "-json", "-no-color"]) or "{}")
        return {k: v.get("value") for k, v in raw.items()}
