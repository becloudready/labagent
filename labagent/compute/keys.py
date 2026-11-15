"""The SSH key a GPU launch logs in with: make one, and prove which public key a private file belongs to.

Uses the system `ssh-keygen`. Nothing here sends a private key anywhere or prints it: callers get the PUBLIC half and its fingerprint.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Tuple

from .errors import ComputeError
from .providers import fingerprint_of, keys_dir

DEFAULT_KEY_NAME = "labagent_ed25519"


def default_key_file() -> str:
    """The one key every cloud shares: ~/.labagent/keys/labagent_ed25519 (under LABAGENT_HOME when that is set). Apart from ~/.ssh on purpose."""
    return str(keys_dir() / DEFAULT_KEY_NAME)


def _keygen(argv, what: str) -> str:
    try:
        r = subprocess.run(["ssh-keygen", *argv], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except FileNotFoundError:
        raise ComputeError("the `ssh-keygen` command is not installed on this machine")
    except subprocess.TimeoutExpired:
        raise ComputeError(f"ssh-keygen took too long to {what}")
    if r.returncode != 0:
        why = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no detail"]
        raise ComputeError(f"ssh-keygen could not {what}: {why[0][:200]}")
    return r.stdout


def public_half(key_file: str) -> str:
    """The public key line of a private key file. A key with a passphrase cannot be used by a job (nothing can type it), so it is refused."""
    path = Path(os.path.expanduser(str(key_file)))
    if not path.is_file():
        raise ComputeError(f"the SSH key file {path} does not exist. Create one: labagent compute ssh-key-add --cloud <cloud> --name <name> --file {path}")
    line = _keygen(["-y", "-P", "", "-f", str(path)], f"read {path}").strip()
    if not fingerprint_of(line):
        raise ComputeError(f"{path} is not an SSH private key (or needs a passphrase, which a launch cannot type)")
    return line


def private_fingerprint(key_file: str) -> str:
    """`SHA256:...` of the public half of this private key file, the same text `ssh-keygen -lf` and `compute ssh-keys` show."""
    return fingerprint_of(public_half(key_file))


def ensure_key(key_file: str, comment: str) -> Tuple[str, bool]:
    """(public key line, created). Creates an ed25519 key with no passphrase at key_file when it does not exist; never overwrites one."""
    path = Path(os.path.expanduser(str(key_file)))
    if path.exists():
        return public_half(str(path)), False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _keygen(["-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)], f"create {path}")
    path.chmod(0o600)
    return public_half(str(path)), True
