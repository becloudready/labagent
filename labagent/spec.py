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

import json
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
    """SafeLoader that reads a value the way the command line and JSON would, so a file never silently means something else.

    Only true/false are booleans (YAML 1.1 would also turn yes/no/on/off into booleans). A number is plain decimal: `007`, `0x10`,
    `1_000` and `1:30` stay text (YAML 1.1 reads them as octal, hex and base 60), and so does a date. Aliases and duplicate keys are
    refused: an alias can blow a small file up into a huge one, and the second of two equal keys would silently win.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.constructor.ConstructorError(None, None, "aliases (*name) are not allowed in labagent files", self.peek_event().start_mark)
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, (str, int, float, bool)) and key in seen:
                raise yaml.constructor.ConstructorError("while reading a mapping", node.start_mark, f"the key {key!r} appears twice", key_node.start_mark)
            if isinstance(key, (str, int, float, bool)):
                seen.add(key)
        return super().construct_mapping(node, deep)


def _resolvers() -> None:
    drop = {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp"}
    _Loader.yaml_implicit_resolvers = {k: [(tag, rx) for tag, rx in v if tag not in drop] for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    _Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    _Loader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), list("-+0123456789"))
    _Loader.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$"), list("-+0123456789."))


_resolvers()


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


def _read(path: str, kind: str) -> str:
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as e:
        raise SpecError(f"{path}: cannot read the file ({e.strerror or type(e).__name__})")
    if len(raw) > MAX_BYTES:
        raise SpecError(f"{path}: the file is larger than {MAX_BYTES // 1000} KB, which is not {kind}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SpecError(f"{path}: the file is not UTF-8 text")


def _yaml(text: str, path: str) -> Any:
    try:
        return yaml.load(text, Loader=_Loader)                   # noqa: S506 (a SafeLoader subclass)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark else ""
        raise SpecError(f"{path}: invalid YAML{line}: {getattr(e, 'problem', None) or 'cannot parse'}")


def is_yaml(path: str) -> bool:
    return str(path).lower().endswith((".yaml", ".yml"))


def load_document(path: str, kind: str = "a labagent file") -> Dict[str, Any]:
    """Read a job, config or ruleset: JSON, or YAML when the file name ends in .yaml or .yml. The top level must be a mapping.

    Unlike a launch file, nothing is expanded and nothing is turned into a string: a number stays a number and `"007"` stays text,
    exactly as the same document in JSON would read. Write an account id or a learner id that is all digits in quotes.
    """
    text = _read(path, kind)
    if is_yaml(path):
        doc = _yaml(text, path)
    else:
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise SpecError(f"{path}: invalid JSON: {e}")
    if not isinstance(doc, dict):
        raise SpecError(f"{path}: the top level must be a mapping (an object), not {'empty' if doc is None else type(doc).__name__}")
    return doc


def load(path: str, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Read a launch file. Returns {"pack": str|None, "name": str|None, "params": {str: str}}."""
    env = dict(os.environ) if env is None else env
    doc = _yaml(_read(path, "a launch file"), path)
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
