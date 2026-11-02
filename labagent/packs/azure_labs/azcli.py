"""Thin wrapper over the `az` CLI. The runner is injectable so tests never touch Azure. Error text is redacted."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from ..base import PackError

SECRET = re.compile(r"(eyJ[\w-]{20,}\.[\w-]{10,}\.[\w-]{10,}|(?i:password|secret|key|token)\S*\s*[=:]\s*\S+)")


def redact(text: str, tail: int = 6) -> str:
    return SECRET.sub("<redacted>", "\n".join(str(text).strip().splitlines()[-tail:]))[:600]


def tag_args(tags: Dict[str, str]) -> List[str]:
    return ["--tags", *[f"{k}={v}" for k, v in tags.items()]]


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

    def json(self, *args: str) -> Any:
        cmd = [self.binary(), *args, "--only-show-errors", "-o", "json"]
        if self.subscription:
            cmd += ["--subscription", self.subscription]
        p = self._run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise PackError(f"az {' '.join(args[:2])} failed: {redact(p.stderr or p.stdout)}")
        return json.loads(p.stdout) if (p.stdout or "").strip() else {}

    # ---- account and capacity ------------------------------------------------------------------------------------------
    def account(self) -> Dict[str, Any]:
        try:
            return self.json("account", "show")
        except PackError as e:
            raise PackError(f"could not read the Azure login. Run: az login ({e})") from None

    def usage(self, region: str) -> Dict[str, Dict[str, int]]:
        rows = self.json("vm", "list-usage", "-l", region)
        return {r["name"]["value"]: {"used": int(r["currentValue"]), "limit": int(r["limit"])} for r in rows}

    def skus(self, region: str) -> Dict[str, Dict[str, Any]]:
        """Every VM size in the region: family, vCPUs, restricted. One call per run (az filters on the client, so it takes about a minute)."""
        cache = self.__dict__.setdefault("_sku_cache", {})
        if region not in cache:
            rows = self.json("vm", "list-skus", "-l", region, "--resource-type", "virtualMachines", "--query",
                             "[].{name:name,family:family,restricted:length(restrictions),vcpus:capabilities[?name=='vCPUs'].value|[0]}")
            cache[region] = {r["name"]: r for r in rows}
        return cache[region]

    # ---- groups and resources -----------------------------------------------------------------------------------------
    def group_exists(self, name: str) -> bool:
        return bool(self.json("group", "exists", "-n", name))

    def group_show(self, name: str) -> Optional[Dict[str, Any]]:
        return self.json("group", "show", "-n", name) if self.group_exists(name) else None

    def group_create(self, name: str, region: str, tags: Dict[str, str]) -> None:
        self.json("group", "create", "-n", name, "-l", region, *tag_args(tags))

    def group_delete(self, name: str) -> None:
        self.json("group", "delete", "-n", name, "--yes")

    def resources(self, group: str) -> List[Dict[str, Any]]:
        return self.json("resource", "list", "-g", group) or []

    def resource_delete(self, rid: str) -> None:
        self.json("resource", "delete", "--ids", rid)

    def show(self, rid: str) -> Dict[str, Any]:
        return self.json("resource", "show", "--ids", rid)

    # ---- reading the source (read-only) ---------------------------------------------------------------------------------
    def list_vms(self, group: str) -> List[Dict[str, Any]]:
        return self.json("vm", "list", "-g", group) or []

    def list_disks(self, group: str) -> List[Dict[str, Any]]:
        return self.json("disk", "list", "-g", group) or []

    def vm_state(self, group: str, name: str) -> Dict[str, Any]:
        return self.json("vm", "show", "-d", "-g", group, "-n", name)

    # ---- creating in the target ---------------------------------------------------------------------------------------
    def snapshot_create(self, group: str, name: str, source_id: str, location: str, tags: Dict[str, str], hyper_v: Optional[str] = None) -> None:
        extra = ["--hyper-v-generation", hyper_v] if hyper_v else []
        self.json("snapshot", "create", "-g", group, "-n", name, "--source", source_id, "-l", location, "--incremental", "false", *extra, *tag_args(tags))

    def disk_create(self, group: str, name: str, snapshot_id: str, sku: str, location: str, tags: Dict[str, str],
                    zone: Optional[str] = None, hyper_v: Optional[str] = None, os_type: Optional[str] = None) -> None:
        extra = (["--zone", zone] if zone else []) + (["--hyper-v-generation", hyper_v] if hyper_v else []) + (["--os-type", os_type] if os_type else [])
        self.json("disk", "create", "-g", group, "-n", name, "--source", snapshot_id, "--sku", sku, "-l", location, *extra, *tag_args(tags))

    def nsg_create(self, group: str, name: str, location: str, tags: Dict[str, str]) -> None:
        self.json("network", "nsg", "create", "-g", group, "-n", name, "-l", location, *tag_args(tags))

    def nsg_rule_create(self, group: str, nsg: str, rule: Dict[str, Any]) -> None:
        args = ["network", "nsg", "rule", "create", "-g", group, "--nsg-name", nsg, "-n", rule["name"], "--priority", str(rule["priority"]),
                "--direction", rule["direction"], "--access", rule["access"], "--protocol", rule["protocol"]]
        for flag, key in (("--source-address-prefixes", "source_addresses"), ("--source-port-ranges", "source_ports"),
                          ("--destination-address-prefixes", "destination_addresses"), ("--destination-port-ranges", "destination_ports")):
            args += [flag, *rule[key]]
        self.json(*args)

    def vnet_create(self, group: str, name: str, prefixes: List[str], subnet: str, subnet_prefix: str, location: str, tags: Dict[str, str]) -> None:
        self.json("network", "vnet", "create", "-g", group, "-n", name, "--address-prefixes", *prefixes, "--subnet-name", subnet,
                  "--subnet-prefixes", subnet_prefix, "-l", location, *tag_args(tags))

    def subnet_create(self, group: str, vnet: str, name: str, prefix: str) -> None:
        self.json("network", "vnet", "subnet", "create", "-g", group, "--vnet-name", vnet, "-n", name, "--address-prefixes", prefix)

    def subnet_set_nsg(self, group: str, vnet: str, subnet: str, nsg: str) -> None:
        self.json("network", "vnet", "subnet", "update", "-g", group, "--vnet-name", vnet, "-n", subnet, "--network-security-group", nsg)

    def public_ip_create(self, group: str, name: str, location: str, tags: Dict[str, str]) -> None:
        self.json("network", "public-ip", "create", "-g", group, "-n", name, "-l", location, "--sku", "Standard", "--allocation-method", "Static", *tag_args(tags))

    def nic_create(self, group: str, name: str, vnet: str, subnet: str, location: str, tags: Dict[str, str],
                   nsg: Optional[str] = None, public_ip: Optional[str] = None, accelerated: bool = False) -> None:
        args = ["network", "nic", "create", "-g", group, "-n", name, "--vnet-name", vnet, "--subnet", subnet, "-l", location]
        if nsg:
            args += ["--network-security-group", nsg]
        if public_ip:
            args += ["--public-ip-address", public_ip]
        if accelerated:
            args += ["--accelerated-networking", "true"]
        self.json(*args, *tag_args(tags))

    def vm_create(self, group: str, name: str, os_disk: str, os_type: str, size: str, nics: List[str], data_disks: List[str],
                  location: str, tags: Dict[str, str], zone: Optional[str] = None) -> None:
        args = ["vm", "create", "-g", group, "-n", name, "--attach-os-disk", os_disk, "--os-type", os_type, "--size", size, "--nics", *nics, "-l", location]
        if data_disks:
            args += ["--attach-data-disks", *data_disks]
        if zone:
            args += ["--zone", zone]
        self.json(*args, *tag_args(tags))
