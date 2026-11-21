"""Local record of what `labagent launch` started, so `labagent destroy NAME` and `labagent status` need no memory from you.

`~/.labagent/instances.json` (or `$LABAGENT_HOME/instances.json`), mode 0600, keyed by the instance name:

    {"demo2": {"name": "demo2", "pack": "gpu-notebooks-container", "params": {"cloud": "runpod", "gpu": "H100", "ttl": "2h"},
               "created_at": "2026-11-21T10:00:00+00:00", "ttl_minutes": 120}}

It is a convenience copy. The pack's own state under `packs/` and the cloud account stay the source of truth: deleting this file
loses only the shortcut, never a machine. Names are the key, so launching a second pack under a name replaces the first entry.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_TTL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.I)
_UNIT_MIN = {"s": 1 / 60, "m": 1, "h": 60, "d": 1440}


class InstanceError(ValueError):
    """The record cannot be read or a duration cannot be understood. The message says what to change."""


def path() -> Path:
    return Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "instances.json"


def parse_ttl(text: Any) -> int:
    """'90m' -> 90, '2h' -> 120, '1d' -> 1440, '1.5h' -> 90. A unit is required (a bare 90 could be minutes or hours). Rounds up to whole minutes."""
    m = _TTL.match(str(text))
    if not m or float(m.group(1)) <= 0:
        raise InstanceError(f"ttl {text!r} is not a duration: use a number and a unit, e.g. 90m, 2h or 1d")
    return max(1, int(math.ceil(float(m.group(1)) * _UNIT_MIN[m.group(2).lower()] - 1e-9)))


def load() -> Dict[str, Dict[str, Any]]:
    p = path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        raise InstanceError(f"{p} is not valid JSON. Fix or delete it (it only holds shortcuts: no machine is lost)")
    if not isinstance(data, dict):
        raise InstanceError(f"{p} must hold a JSON object keyed by instance name")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write(data: Dict[str, Dict[str, Any]]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".instances.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)                       # a crash leaves the old file, never half of a new one
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def get(name: str) -> Optional[Dict[str, Any]]:
    return load().get(name)


def record(name: str, pack: str, params: Dict[str, Any], ttl_minutes: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Save or update the entry for `name`. A rerun keeps the original created_at: the machine is the same one."""
    data = load()
    old = data.get(name) or {}
    created = old.get("created_at") if old.get("pack") == pack else None
    entry = {"name": name, "pack": pack, "params": {k: v for k, v in params.items()},
             "created_at": created or (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat(), "ttl_minutes": ttl_minutes}
    data[name] = entry
    _write(data)
    return entry


def forget(name: str) -> bool:
    data = load()
    if name not in data:
        return False
    del data[name]
    _write(data)
    return True


def _span(minutes: float) -> str:
    m = int(minutes)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def rows(now: Optional[datetime] = None, defaults: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """One row per instance for `status`. `defaults` maps a pack id to its default cloud/gpu, used when the launch did not set them."""
    now = now or datetime.now(timezone.utc)
    out = []
    for name, e in sorted(load().items()):
        p = e.get("params") or {}
        d = (defaults or {}).get(e.get("pack"), {})
        try:
            started = datetime.fromisoformat(e["created_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = max((now - started).total_seconds() / 60, 0)
        except (KeyError, TypeError, ValueError):
            elapsed = None
        ttl = e.get("ttl_minutes")
        left = None if ttl is None or elapsed is None else ttl - elapsed
        out.append({"name": name, "pack": e.get("pack"), "cloud": p.get("cloud") or d.get("cloud"), "gpu": p.get("gpu") or p.get("instance_type") or d.get("gpu"),
                    "created_at": e.get("created_at"), "elapsed_minutes": None if elapsed is None else int(elapsed), "ttl_minutes": ttl,
                    "ttl_remaining_minutes": None if left is None else int(math.ceil(left))})
    return out


def table(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "No instances recorded. Launch one: labagent launch <pack> --name NAME --cloud CLOUD --gpu H100"
    head = ("NAME", "PACK", "CLOUD", "GPU", "ELAPSED", "TTL REMAINING")
    body = []
    for i in items:
        left = i["ttl_remaining_minutes"]
        body.append((i["name"], i["pack"] or "-", i["cloud"] or "-", i["gpu"] or "-",
                     "-" if i["elapsed_minutes"] is None else _span(i["elapsed_minutes"]),
                     "-" if left is None else ("expired" if left <= 0 else _span(left))))
    w = [max(len(str(r[c])) for r in [head] + body) for c in range(len(head))]
    return "\n".join("  ".join(str(r[c]).ljust(w[c]) for c in range(len(head))).rstrip() for r in [head] + body)
