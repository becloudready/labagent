"""Guards for azure-labs: subscription allow-list, names, tags, size and count caps.

Same standard as the AWS engine: nothing runs unless the subscription is allowed, every resource is tagged, and
destroy only touches resources that carry this lab's tags.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..base import PackError

PACK_ID = "azure-labs"
ALLOWED_ENV = "LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS"
DEFAULT_MAX_STUDENTS = 30
STUDENT_CEILING = 100
NEVER = "never"

# size -> vCPUs. Only these may be used. The first group is the general list; NESTED is the subset for nested=true.
SIZES: Dict[str, int] = {
    "Standard_B1s": 1, "Standard_B2s": 2, "Standard_B2ms": 2,
    "Standard_D2s_v3": 2, "Standard_D4s_v3": 4, "Standard_D2s_v5": 2, "Standard_D4s_v5": 4,
    "Standard_D8s_v5": 8, "Standard_E4s_v5": 4,
}
NESTED = {"Standard_D2s_v3", "Standard_D4s_v3", "Standard_D4s_v5", "Standard_D8s_v5", "Standard_E4s_v5"}
DEFAULT_SIZE = {"linux": "Standard_B2s", "windows-server": "Standard_D2s_v3"}
GATEWAY_SIZE = "Standard_B2s"
OS_CHOICES = tuple(DEFAULT_SIZE)


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")[:n].strip("-")


def lab_name(name: str) -> str:
    s = slug(name)
    if len(s) < 3:
        raise PackError("the lab name must have at least 3 letters or digits after cleaning (lower case, a-z, 0-9 and -)")
    return s


def group_name(name: str) -> str:
    return f"rg-labagent-{PACK_ID}-{lab_name(name)}"


def images_group_name(name: str) -> str:
    return group_name(name) + "-images"


def allowed_subscriptions(environ: Optional[Dict[str, str]] = None) -> List[str]:
    raw = (environ if environ is not None else os.environ).get(ALLOWED_ENV, "")
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def check_subscription(subscription_id: str, environ: Optional[Dict[str, str]] = None) -> None:
    """Refuse to run unless the subscription is explicitly allowed. An empty list refuses everything."""
    allowed = allowed_subscriptions(environ)
    if not allowed:
        raise PackError(f"{ALLOWED_ENV} is empty: refusing to run. Set it to the subscription ID you may use for labs.")
    if str(subscription_id).lower() not in allowed:
        raise PackError(f"the current Azure subscription is not in {ALLOWED_ENV}: refusing to run.")


def validate(params: Dict[str, Any]) -> Dict[str, Any]:
    """Check and normalise the parameters. Returns the values the steps use."""
    os_name = str(params.get("os") or "linux")
    if os_name not in OS_CHOICES:
        raise PackError(f"os must be one of: {', '.join(OS_CHOICES)}")
    nested = bool(params.get("nested"))
    size = params.get("vm_size") or ("Standard_D4s_v3" if nested else DEFAULT_SIZE[os_name])
    if size not in SIZES:
        raise PackError(f"vm_size {size} is not allowed. Allowed: {', '.join(sorted(SIZES))}")
    if nested and size not in NESTED:
        raise PackError(f"nested=true needs a nested-virtualization size: {', '.join(sorted(NESTED))}")
    try:
        students = int(params.get("students"))
        cap = int(params.get("max_students") or DEFAULT_MAX_STUDENTS)
    except (TypeError, ValueError):
        raise PackError("students and max_students must be whole numbers")
    if not 1 <= cap <= STUDENT_CEILING:
        raise PackError(f"max_students must be between 1 and {STUDENT_CEILING}")
    if not 1 <= students <= cap:
        raise PackError(f"students must be between 1 and max_students ({cap})")
    region = str(params.get("region") or "")
    if not re.fullmatch(r"[a-z0-9]{3,30}", region):
        raise PackError("region must look like canadacentral")
    try:
        days = int(params.get("expires_in_days"))
    except (TypeError, ValueError):
        raise PackError("expires_in_days must be a whole number (0 means never)")
    if not 0 <= days <= 365:
        raise PackError("expires_in_days must be between 0 and 365")
    return {"os": os_name, "vm_size": size, "vcpus": SIZES[size], "nested": nested, "students": students,
            "max_students": cap, "region": region, "expires_in_days": days}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def lab_tags(name: str, cohort: str, days: int, now: Optional[datetime] = None, created_group: bool = False) -> Dict[str, str]:
    now = now or utc_now()
    tags = {"labagent": "1", "pack": PACK_ID, "name": lab_name(name), "cohort": slug(cohort or name) or lab_name(name),
            "created_at": stamp(now), "expires_at": stamp(now + timedelta(days=days)) if days else NEVER}
    if created_group:
        tags["created_by_labagent"] = "1"
    return tags


def owned(tags: Optional[Dict[str, str]], name: str) -> bool:
    """True only for a resource that carries this lab's tags. Untagged or differently tagged resources are never ours."""
    t = tags or {}
    return t.get("labagent") == "1" and t.get("pack") == PACK_ID and t.get("name") == lab_name(name)


def is_labagent_group(tags: Optional[Dict[str, str]]) -> bool:
    t = tags or {}
    return t.get("labagent") == "1" and t.get("pack") == PACK_ID
