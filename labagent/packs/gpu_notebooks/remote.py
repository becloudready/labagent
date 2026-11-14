"""Run commands on the GPU VM over the system `ssh`. Nothing secret goes on a command line: tokens travel on stdin."""
from __future__ import annotations

import subprocess
from typing import Optional, Tuple

from ..base import PackError


class SshRunner:
    def __init__(self, host: str, user: str = "shadeform", port: int = 22, key_file: Optional[str] = None, known_hosts: Optional[str] = None):
        self.host, self.user, self.port, self.key_file, self.known_hosts = host, user, int(port or 22), key_file, known_hosts

    def _argv(self, command: str) -> list:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "StrictHostKeyChecking=accept-new",
                "-p", str(self.port)]
        if self.known_hosts:        # one file per VM: a cloud reuses IP addresses, and an old entry would block a new VM
            argv += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        if self.key_file:
            argv += ["-i", self.key_file, "-o", "IdentitiesOnly=yes"]
        return argv + [f"{self.user}@{self.host}", command]

    def run(self, command: str, stdin: Optional[str] = None, timeout: int = 900) -> Tuple[int, str, str]:
        """(exit code, stdout, stderr). The command string is run by the remote shell."""
        try:
            r = subprocess.run(self._argv(command), input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError:
            raise PackError("the `ssh` command is not installed on this machine")
        except subprocess.TimeoutExpired:
            raise PackError(f"the command on the VM took longer than {timeout} s")
        return r.returncode, r.stdout, r.stderr
