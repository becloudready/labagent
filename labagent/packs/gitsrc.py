"""Fetch a public git repo at an exact commit. The pinned SHA is the integrity check: git verifies content against it,
and the result is refused if HEAD differs. Nothing from the repo is vendored into labagent."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .base import PackError

SHA = re.compile(r"^[0-9a-f]{40}$")
URL = re.compile(r"^https://[\w.-]+/[\w.-]+/[\w.-]+?(\.git)?/?$")


def clone_pinned(url: str, sha: str, dest: Path, run: Callable = subprocess.run) -> Path:
    if not URL.match(url):
        raise PackError("repo must be an https URL like https://github.com/org/name")
    if not SHA.match(sha):
        raise PackError("ref must be a full 40-character commit SHA (a branch or tag name is not a pin)")
    git = shutil.which("git")
    if not git:
        raise PackError("git is required to fetch the app source")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull}

    def g(*args: str, cwd: Path) -> str:
        p = run([git, *args], cwd=str(cwd), capture_output=True, text=True, env=env)
        if p.returncode != 0:
            raise PackError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip().splitlines()[-1:][0] if (p.stderr or p.stdout).strip() else ''}")
        return p.stdout.strip()

    if (dest / ".git").exists() and g("rev-parse", "HEAD", cwd=dest) == sha:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    g("init", "-q", cwd=dest)
    g("fetch", "-q", "--depth", "1", url, sha, cwd=dest)
    g("checkout", "-q", "FETCH_HEAD", cwd=dest)
    if g("rev-parse", "HEAD", cwd=dest) != sha:
        shutil.rmtree(dest, ignore_errors=True)
        raise PackError("fetched commit does not match the pinned SHA: refusing to use it")
    return dest
