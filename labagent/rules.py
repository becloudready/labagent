"""Ruleset: the explicit, reviewable statement of what may be deleted from an existing account.

Decision order (safety first):
  1. any `keep` rule that matches          -> keep   (always wins)
  2. built-in protected name prefixes      -> keep
  3. a `delete` rule that matches          -> delete (only if labagent has a deleter for the type)
  4. anything else                         -> review (reported, NEVER deleted)

Ruleset JSON:
{
  "version": 1, "name": "legacy-cleanup",
  "rules": [
    {"id": "keep-admins", "action": "keep",   "match": {"type": ["iam/user"], "name": ["admin-*"]}},
    {"id": "old-keys",    "action": "delete", "match": {"type": ["ec2/key-pair"], "older_than_days": 30}}
  ]
}
Match keys (all must match; a list means any of its entries):
A rule may carry "enabled": false to stay in the file without taking effect (review it, then flip it on).
  type            ["service/type", ...]      region [glob]      name [glob]      name_regex "regex"      id [glob]
  tags            {"Key": "glob"} ("*" = present)       tags_absent ["Key"]
  older_than_days n   (attrs.created)       unused_days n (attrs.last_used, never-used counts if older than n days)
  attrs           {"enabled": true, "attachments": 0, ...}  exact match on discovered attributes
                  (default VPCs are never deleted by a broad rule: the rule must include "is_default": true)
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import Config
from .discovery import Resource

VERSION = 1


class RulesetError(Exception):
    pass


@dataclass
class Decision:
    action: str            # keep | delete | review
    rule: Optional[str]
    reason: str


KNOWN_MATCH = {"type", "region", "name", "name_regex", "id", "tags", "tags_absent", "older_than_days",
               "unused_days", "attrs"}


def validate(ruleset: Dict[str, Any]) -> None:
    if ruleset.get("version") != VERSION:
        raise RulesetError(f"ruleset version must be {VERSION}")
    seen = set()
    for r in ruleset.get("rules", []):
        if r.get("id") in seen or not r.get("id"):
            raise RulesetError(f"rule ids must be present and unique: {r.get('id')!r}")
        seen.add(r["id"])
        if r.get("action") not in ("keep", "delete"):
            raise RulesetError(f"rule {r['id']}: action must be keep or delete")
        m = r.get("match")
        if not isinstance(m, dict) or not m:
            raise RulesetError(f"rule {r['id']}: an empty match would match everything; refusing")
        unknown = set(m) - KNOWN_MATCH
        if unknown:
            raise RulesetError(f"rule {r['id']}: unknown match keys {sorted(unknown)}")
        if r["action"] == "delete" and not ({"type"} & set(m)):
            raise RulesetError(f"rule {r['id']}: delete rules must name at least one type")
        if "name_regex" in m:
            re.compile(m["name_regex"])


def _as_list(v) -> List[Any]:
    return v if isinstance(v, list) else [v]


def _parse_dt(v: Any) -> Optional[datetime]:
    """Accepts datetime strings as boto3 renders them, ISO 8601 with or without a colon in the offset,
    and epoch milliseconds (CloudWatch log groups)."""
    if v in (None, "", "None"):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return datetime.fromtimestamp(int(s) / (1000 if len(s) > 10 else 1), tz=timezone.utc)
    s = s.replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _age_days(res: Resource, key: str, now: datetime) -> Optional[float]:
    dt = _parse_dt(res.attrs.get(key))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400


def matches(res: Resource, m: Dict[str, Any], now: datetime) -> bool:
    if "type" in m and f"{res.service}/{res.rtype}" not in _as_list(m["type"]):
        return False
    if "region" in m and not any(fnmatch.fnmatchcase(res.region, g) for g in _as_list(m["region"])):
        return False
    if "name" in m and not any(fnmatch.fnmatchcase(res.name.lower(), g.lower()) for g in _as_list(m["name"])):
        return False
    if "id" in m and not any(fnmatch.fnmatchcase(res.rid.lower(), g.lower()) for g in _as_list(m["id"])):
        return False
    if "name_regex" in m and not re.search(m["name_regex"], res.name, re.I):
        return False
    for k, g in (m.get("tags") or {}).items():
        v = next((tv for tk, tv in res.tags.items() if tk.lower() == k.lower()), None)
        if v is None or not fnmatch.fnmatchcase(v.lower(), str(g).lower()):
            return False
    if any(k.lower() in {t.lower() for t in res.tags} for k in m.get("tags_absent", [])):
        return False
    if "older_than_days" in m:
        age = _age_days(res, "created", now)
        if age is None or age < m["older_than_days"]:
            return False
    if "unused_days" in m:
        used = _age_days(res, "last_used", now)
        if used is None:                                   # never used: judge by creation age
            created = _age_days(res, "created", now)
            if created is None or created < m["unused_days"]:
                return False
        elif used < m["unused_days"]:
            return False
    for k, v in (m.get("attrs") or {}).items():
        if res.attrs.get(k) != v:
            return False
    return True


def decide(res: Resource, ruleset: Dict[str, Any], cfg: Config, now: Optional[datetime] = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    rules: List[Dict[str, Any]] = [r for r in ruleset.get("rules", []) if r.get("enabled", True)]
    for r in rules:
        if r["action"] == "keep" and matches(res, r["match"], now):
            return Decision("keep", r["id"], r.get("reason", "protected by keep rule"))
    if any(res.name.startswith(p) or res.rid.startswith(p) for p in cfg.protected_name_prefixes):
        return Decision("keep", "builtin-protected-prefix", "protected name prefix")
    for r in rules:
        if r["action"] == "delete" and matches(res, r["match"], now):
            if res.attrs.get("is_default") and "is_default" not in (r["match"].get("attrs") or {}):
                return Decision("review", r["id"], "default VPC: a delete rule must say attrs.is_default explicitly")
            return Decision("delete", r["id"], r.get("reason", "matched delete rule"))
    return Decision("review", None, "no rule matched: needs a human decision")
