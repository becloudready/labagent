"""Thin, safe wrapper around the terraform (or tofu) CLI for one cohort's IAM layer.

- Backend (S3 bucket, KMS key) comes from config/env at `init` time: nothing org-specific is committed.
- One state per cohort: cohorts/<cohort>/terraform.tfstate.
- The roster is written to a 0600 var-file in a temp dir that is removed afterwards.
- Terraform output is never logged: it contains usernames. Errors are redacted to ids and a short tail.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import MODULE_DIR, Config, getenv

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class TerraformError(Exception):
    pass


def redact(text: str, tail: int = 12) -> str:
    lines = [EMAIL.sub("<redacted>", ln) for ln in text.strip().splitlines()[-tail:]]
    return "\n".join(lines)[:1500]


class Terraform:
    def __init__(self, cfg: Config, module_dir: Optional[Path] = None, run=subprocess.run):
        self.cfg = cfg
        self.module_dir = Path(module_dir or MODULE_DIR)
        self._run = run

    # -- plumbing ------------------------------------------------------------
    def binary(self) -> str:
        b = self.cfg.terraform_bin or getenv("TF_BIN") or shutil.which("terraform") or shutil.which("tofu")
        if not b:
            raise TerraformError("terraform/tofu binary not found: install one or set terraform_bin / LABAGENT_TF_BIN")
        return b

    def _exec(self, args: List[str], data_dir: Path, env_extra: Optional[Dict[str, str]] = None) -> str:
        env = {**os.environ, "TF_DATA_DIR": str(data_dir), "TF_IN_AUTOMATION": "1", "TF_INPUT": "0",
               "CHECKPOINT_DISABLE": "1", **(env_extra or {})}
        cmd = [self.binary(), f"-chdir={self.module_dir}", *args]
        p = self._run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if p.returncode != 0:
            raise TerraformError(f"terraform {args[0]} failed (exit {p.returncode}): {redact(p.stderr or p.stdout)}")
        return p.stdout

    @contextmanager
    def _workspace(self, cohort: str, variables: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        b = self.cfg.backend_settings()
        for need in ("bucket", "region"):
            if not b.get(need):
                raise TerraformError(f"backend.{need} is not configured (config 'backend' or LABAGENT_TF_BACKEND_*)")
        with tempfile.TemporaryDirectory(prefix="labagent-") as tmp:
            tmp_path = Path(tmp)
            varfile = tmp_path / "vars.json"
            fd = os.open(varfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(variables, f)
            data_dir = tmp_path / "data"
            data_dir.mkdir()
            backend_args = [f"-backend-config=bucket={b['bucket']}", f"-backend-config=region={b['region']}",
                            f"-backend-config=key={b['key_template'].format(cohort=cohort)}",
                            "-backend-config=encrypt=true",
                            f"-backend-config=use_lockfile={'true' if b.get('use_lockfile', True) else 'false'}"]
            if b.get("kms_key_id"):
                backend_args.append(f"-backend-config=kms_key_id={b['kms_key_id']}")
            self._exec(["init", "-input=false", "-no-color", "-reconfigure", "-lockfile=readonly", *backend_args], data_dir)
            yield {"data_dir": data_dir, "varfile": varfile, "tmp": tmp_path}

    @staticmethod
    def variables(cfg: Config, cohort: str, created_date: str, learners: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "region": cfg.region, "cohort": cohort, "created_date": created_date,
            "workshop": cfg.workshop, "name_prefix": cfg.name_prefix,
            "learners": {str(l["learner_id"]): {"username": l["username"]} for l in learners},
        }

    # -- operations ----------------------------------------------------------
    def plan(self, cohort: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Read-only. Returns {'create': n, 'update': n, 'delete': n, 'users_to_create': [learner_id]}."""
        with self._workspace(cohort, variables) as w:
            planfile = w["tmp"] / "plan.bin"
            # Read-only: no state lock needed and nothing is written to the backend.
            self._exec(["plan", "-input=false", "-no-color", "-lock=false",
                        f"-var-file={w['varfile']}", f"-out={planfile}"], w["data_dir"])
            shown = json.loads(self._exec(["show", "-json", str(planfile)], w["data_dir"]))
        counts = {"create": 0, "update": 0, "delete": 0}
        users: List[str] = []
        for rc in shown.get("resource_changes", []):
            acts = rc["change"]["actions"]
            for a in counts:
                if a in acts:
                    counts[a] += 1
            if rc["type"] == "aws_iam_user" and "create" in acts:
                users.append(str(rc["index"]))
        counts["users_to_create"] = sorted(users)
        return counts

    def apply(self, cohort: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        with self._workspace(cohort, variables) as w:
            self._exec(["apply", "-auto-approve", "-input=false", "-no-color", "-lock-timeout=5m",
                        f"-var-file={w['varfile']}"], w["data_dir"])
            raw = json.loads(self._exec(["output", "-json"], w["data_dir"]))
        return {k: v["value"] for k, v in raw.items()}

    def destroy(self, cohort: str, variables: Dict[str, Any]) -> None:
        with self._workspace(cohort, variables) as w:
            self._exec(["destroy", "-auto-approve", "-input=false", "-no-color", "-lock-timeout=5m",
                        f"-var-file={w['varfile']}"], w["data_dir"])
