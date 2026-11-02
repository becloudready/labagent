"""Thin wrapper over the `az` CLI. The runner is injectable so tests never touch Azure.

Secrets never go on a command line: values that must stay secret are written to a 0600 temp file for the deployment
parameters and deleted straight after. Error text is redacted before it reaches a log or the result envelope.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from ..base import PackError

SECRET = re.compile(r"(eyJ[\w-]{20,}\.[\w-]{10,}\.[\w-]{10,}|(?i:password|secret|key|token)\S*\s*[=:]\s*\S+)")
SCHEMA = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"


def redact(text: str, tail: int = 6) -> str:
    return SECRET.sub("<redacted>", "\n".join(str(text).strip().splitlines()[-tail:]))[:600]


class AzCli:
    def __init__(self, subscription: Optional[str] = None, run=subprocess.run, binary: Optional[str] = None):
        self.subscription = subscription
        self._run = run
        self._bin = binary

    def binary(self) -> str:
        b = self._bin or shutil.which("az")
        if not b:
            raise PackError("Azure CLI not found. Install it (https://learn.microsoft.com/cli/azure/install-azure-cli) and run: az login")
        return b

    def _exec(self, args: List[str]):
        cmd = [self.binary(), *args, "--only-show-errors", "-o", "json"]
        if self.subscription:
            cmd += ["--subscription", self.subscription]
        return self._run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    def json(self, *args: str) -> Any:
        p = self._exec(list(args))
        if p.returncode != 0:
            raise PackError(f"az {' '.join(args[:2])} failed: {redact(p.stderr or p.stdout)}")
        return json.loads(p.stdout) if (p.stdout or "").strip() else {}

    # ---- account and capacity -------------------------------------------------------------------------------------------
    def account(self) -> Dict[str, Any]:
        """The signed-in subscription: id, name. Raises a clear message when `az login` has not been run."""
        try:
            return self.json("account", "show")
        except PackError as e:
            raise PackError(f"could not read the Azure login. Run: az login ({e})") from None

    def usage(self, region: str) -> Dict[str, Dict[str, int]]:
        """{usage name: {used, limit}} for the region. vCPU totals are 'cores'; each VM family has its own key."""
        rows = self.json("vm", "list-usage", "-l", region)
        return {r["name"]["value"]: {"used": int(r["currentValue"]), "limit": int(r["limit"])} for r in rows}

    def sku_family(self, size: str, region: str) -> str:
        rows = self.json("vm", "list-skus", "-l", region, "--size", size, "--resource-type", "virtualMachines")
        for r in rows:
            if r.get("name") == size:
                if r.get("restrictions"):
                    raise PackError(f"{size} is restricted in {region} for this subscription. Pick another size or region.")
                return r["family"]
        raise PackError(f"{size} is not available in {region}")

    # ---- groups and resources -----------------------------------------------------------------------------------------
    def group_exists(self, name: str) -> bool:
        return bool(self.json("group", "exists", "-n", name))

    def group_show(self, name: str) -> Optional[Dict[str, Any]]:
        return self.json("group", "show", "-n", name) if self.group_exists(name) else None

    def group_create(self, name: str, region: str, tags: Dict[str, str]) -> None:
        self.json("group", "create", "-n", name, "-l", region, "--tags", *[f"{k}={v}" for k, v in tags.items()])

    def group_delete(self, name: str) -> None:
        self.json("group", "delete", "-n", name, "--yes")

    def groups_with_tag(self, key: str, value: str) -> List[Dict[str, Any]]:
        return self.json("group", "list", "--tag", f"{key}={value}") or []

    def resources(self, group: str) -> List[Dict[str, Any]]:
        return self.json("resource", "list", "-g", group) or []

    def resource_delete(self, rid: str) -> None:
        self.json("resource", "delete", "--ids", rid)

    # ---- deployments (ARM templates) ------------------------------------------------------------------------------------
    def deploy(self, group: str, template: str, parameters: Dict[str, Any], name: str) -> Dict[str, Any]:
        """Deploy an ARM template. Parameters go in a 0600 temp file, never on the command line, and the file is removed."""
        body = {"$schema": SCHEMA, "contentVersion": "1.0.0.0", "parameters": {k: {"value": v} for k, v in parameters.items()}}
        fd, path = tempfile.mkstemp(prefix="labagent-az-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(body))
            return self.json("deployment", "group", "create", "-g", group, "-n", name, "--template-file", template, "--parameters", f"@{path}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
