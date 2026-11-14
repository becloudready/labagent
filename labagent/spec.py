"""Launch files: a YAML description of one pack run, so a long command line becomes a file you can keep, review and reuse.

    pack: gpu-notebooks
    name: nvidia-h100-hackathon
    params:
      vendor: scaleway
      region: paris-france-1
      ssh_key_id: ${SHADEFORM_SSH_KEY_ID}
      hours: 6

`labagent launch -f file.yaml` and `labagent destroy -f file.yaml` use the same file. Flags on the command line win over the file.

A file behaves exactly like the same `--param key=value` flags, because every value becomes the string the command line would
have given: `hours: 6` is "6", `true` is "true", a list is a comma list. Only `true` and `false` are booleans: YAML's `yes`, `no`,
`on` and `off` stay text, so `apply: yes` still means what `--param apply=yes` means.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

ALLOWED = ("pack", "name", "params", "description")
MAX_BYTES = 1_000_000
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class SpecError(ValueError):
    """A launch file that cannot be used. The message names the file and says what to change."""


class _Loader(yaml.SafeLoader):
    """SafeLoader where only true/false are booleans (YAML 1.1 would also turn yes/no/on/off into booleans)."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"] for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def _expand(text: str, where: str, env: Dict[str, str]) -> str:
    def sub(m: "re.Match[str]") -> str:
        name, default = m.group(1), m.group(2)
        if env.get(name):
            return env[name]
        if default is not None:
            return default
        raise SpecError(f"{where}: ${{{name}}} is not set. Export {name}, or write ${{{name}:-default}}")
    return _VAR.sub(sub, text)


def _scalar(value: Any, where: str, env: Dict[str, str]) -> Optional[str]:
    """The string the command line would have carried, or None for 'not set'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _expand(value, where, env)
    if hasattr(value, "isoformat"):                    # an unquoted date such as 2026-11-13
        return value.isoformat()
    raise SpecError(f"{where}: a parameter must be a plain value or a list of plain values, not {type(value).__name__}")


def load(path: str, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Read a launch file. Returns {"pack": str|None, "name": str|None, "params": {str: str}}."""
    env = dict(os.environ) if env is None else env
    p = Path(path).expanduser()
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise SpecError(f"{path}: cannot read the file ({e.strerror or type(e).__name__})")
    if len(raw) > MAX_BYTES:
        raise SpecError(f"{path}: the file is larger than {MAX_BYTES // 1000} KB, which is not a launch file")
    try:
        doc = yaml.load(raw.decode("utf-8"), Loader=_Loader)       # noqa: S506 (a SafeLoader subclass)
    except UnicodeDecodeError:
        raise SpecError(f"{path}: the file is not UTF-8 text")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark else ""
        raise SpecError(f"{path}: invalid YAML{line}: {getattr(e, 'problem', None) or 'cannot parse'}")
    if doc is None:
        raise SpecError(f"{path}: the file is empty. It needs at least `pack: <name>`")
    if not isinstance(doc, dict):
        raise SpecError(f"{path}: the top level must be a mapping with pack, name and params")
    unknown = sorted(str(k) for k in doc if k not in ALLOWED)
    if unknown:
        raise SpecError(f"{path}: unknown key(s) {', '.join(unknown)}. Allowed: {', '.join(ALLOWED)}")
    out: Dict[str, Any] = {"pack": None, "name": None, "params": {}}
    for key in ("pack", "name"):
        if doc.get(key) is not None:
            v = _scalar(doc[key], f"{path}: {key}", env)
            if not v or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v):
                raise SpecError(f"{path}: {key} must be letters, digits, dot, dash or underscore, got {v!r}")
            out[key] = v
    params = doc.get("params")
    if params is not None and not isinstance(params, dict):
        raise SpecError(f"{path}: params must be a mapping of name: value")
    for k, v in (params or {}).items():
        where = f"{path}: params.{k}"
        if isinstance(v, list):
            parts = [_scalar(x, where, env) for x in v]
            if any(x is None or isinstance(x, (list, dict)) for x in parts):
                raise SpecError(f"{where}: list items must be plain values")
            val: Optional[str] = ",".join(str(x) for x in parts)
        else:
            val = _scalar(v, where, env)
        if val is not None:                             # `key:` with nothing after it means "use the pack's default"
            out["params"][str(k)] = val
    return out
