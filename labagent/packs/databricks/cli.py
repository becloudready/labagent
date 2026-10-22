"""Thin wrapper over the Databricks CLI. The runner is injectable so tests never touch a workspace."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any, List, Optional

from ..base import PackError

SECRET = re.compile(r"(dapi[0-9a-f]{20,}|eyJ[\w-]{20,}\.[\w-]{10,}\.[\w-]{10,})")


def redact(text: str, tail: int = 8) -> str:
    return SECRET.sub("<redacted>", "\n".join(text.strip().splitlines()[-tail:]))[:1200]


class Dbx:
    def __init__(self, profile: Optional[str] = None, run=subprocess.run, binary: Optional[str] = None, sleep=time.sleep):
        self.profile = profile
        self._run = run
        self._sleep = sleep
        self._bin = binary

    def binary(self) -> str:
        b = self._bin or shutil.which("databricks")
        if not b:
            raise PackError("Databricks CLI not found. Install it (brew install databricks) and run: databricks auth login")
        return b

    def text(self, *args: str, stdin: Optional[str] = None) -> str:
        cmd = [self.binary(), *args]
        if self.profile:
            cmd += ["--profile", self.profile]
        p = self._run(cmd, capture_output=True, text=True, input=stdin)
        if p.returncode != 0:
            raise PackError(f"databricks {args[0]} {args[1] if len(args) > 1 else ''} failed: {redact(p.stderr or p.stdout)}")
        return p.stdout

    def json(self, *args: str, body: Any = None) -> Any:
        extra: List[str] = ["--json", json.dumps(body)] if body is not None else []
        out = self.text(*args, *extra, "-o", "json")
        return json.loads(out) if out.strip() else {}

    def api(self, method: str, path: str, body: Any = None, retries: int = 6) -> Any:
        """Rate limits are normal on Free Edition foundation-model endpoints: back off 2s, 4s, 8s... then give up."""
        extra = ["--json", json.dumps(body)] if body is not None else []
        for attempt in range(retries + 1):
            try:
                out = self.text("api", method, path, *extra)
                break
            except PackError as e:
                if attempt == retries or not any(k in str(e) for k in ("REQUEST_LIMIT_EXCEEDED", "RATE_LIMIT", "429")):
                    raise
                self._sleep(min(2 ** (attempt + 1), 30))
        return json.loads(out) if out.strip() else {}

    def exists(self, *args: str) -> bool:
        try:
            self.text(*args)
            return True
        except PackError:
            return False
