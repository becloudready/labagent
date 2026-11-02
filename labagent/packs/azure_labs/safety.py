"""Guards for azure-labs: the subscription allow-list, names and tags. Destroy only touches what carries this migration's tags."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..base import PackError

PACK_ID = "azure-labs"
ALLOWED_ENV = "LABAGENT_ALLOWED_AZURE_SUBSCRIPTIONS"


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")[:n].strip("-")


def lab_name(name: str) -> str:
    s = slug(name)
    if len(s) < 3:
        raise PackError("the name must have at least 3 letters or digits (a-z, 0-9 and -)")
    return s


def allowed_subscriptions(environ: Optional[Dict[str, str]] = None) -> List[str]:
    raw = (environ if environ is not None else os.environ).get(ALLOWED_ENV, "")
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def check_subscription(subscription_id: str, environ: Optional[Dict[str, str]] = None) -> None:
    """Refuse to run unless the subscription is explicitly allowed. An empty list refuses everything."""
    allowed = allowed_subscriptions(environ)
    if not allowed:
        raise PackError(f"{ALLOWED_ENV} is empty: refusing to run. Set it to the subscription ID you may use.")
    if str(subscription_id).lower() not in allowed:
        raise PackError(f"the current Azure subscription is not in {ALLOWED_ENV}: refusing to run.")


def tags(name: str, source_rg: str, created_group: bool = False, now: Optional[datetime] = None) -> Dict[str, str]:
    now = now or datetime.now(timezone.utc)
    t = {"labagent": "1", "pack": PACK_ID, "name": lab_name(name), "migrated_from": source_rg, "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if created_group:
        t["created_by_labagent"] = "1"
    return t


def owned(resource_tags: Optional[Dict[str, str]], name: str) -> bool:
    """True only for a resource that carries this migration's tags. Anything else is never ours to delete."""
    t = resource_tags or {}
    return t.get("labagent") == "1" and t.get("pack") == PACK_ID and t.get("name") == lab_name(name)
