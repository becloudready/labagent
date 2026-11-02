"""`labagent sweep-azure`: delete labs past their expires_at. Plan first (read-only), then apply exactly that plan."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .envelope import Envelope
from .packs.azure_labs import lifecycle, safety
from .packs.base import PackError
from .plan import PlanError, build_plan, verify_plan

PLAN_MAX_AGE_S = 24 * 3600


def _expired(tags: Dict[str, str], now: datetime) -> Optional[str]:
    """The expiry stamp if this lab is a labagent lab, is not an images group, and is past its expiry. Else None."""
    if not safety.is_labagent_group(tags) or tags.get("role") == "images":
        return None
    when = safety.parse_stamp(tags.get("expires_at"))
    return tags["expires_at"] if when and when <= now else None


def sweep_plan(az: Any, subscription_id: str, now: Optional[datetime] = None) -> Envelope:
    safety.check_subscription(subscription_id)
    now = now or safety.utc_now()
    env = Envelope(command="sweep-azure", mode="plan", cohort="azure")
    items: List[Dict[str, Any]] = []
    for g in az.groups_with_tag("labagent", "1"):
        tags = g.get("tags") or {}
        if not safety.is_labagent_group(tags):
            continue
        stamp = _expired(tags, now)
        items.append({"arn": g["id"], "service": "resourcegroup", "type": "group", "id": g["name"], "region": g.get("location"),
                      "name": tags.get("name"), "action": "delete" if stamp else "skip",
                      "reason": f"expired {stamp}" if stamp else ("images group" if tags.get("role") == "images" else "not expired"),
                      "state": "planned"})
    env.items = items
    env.plan = build_plan("azure", subscription_id, items, now.timestamp())
    n = sum(1 for i in items if i["action"] == "delete")
    env.summary = {"to_delete": n, "skipped": len(items) - n}
    return env


def sweep_apply(az: Any, subscription_id: str, plan: Dict[str, Any], approved_hash: Optional[str], now: Optional[datetime] = None,
                log: Callable[[str], None] = lambda m: None) -> Envelope:
    safety.check_subscription(subscription_id)
    now = now or safety.utc_now()
    env = Envelope(command="sweep-azure", mode="apply", cohort="azure")
    try:
        verify_plan(plan, subscription_id, now.timestamp(), PLAN_MAX_AGE_S, approved_hash)
    except PlanError as e:
        return env.fail(str(e))
    env.plan = {"id": plan.get("id"), "hash": plan["hash"], "created_at": plan["created_at"]}
    for item in plan["items"]:
        row = dict(item)
        env.items.append(row)
        if item["action"] != "delete":
            row["state"] = "skipped"
            continue
        info = az.group_show(item["id"])             # re-read: a lab whose tags or expiry changed since the plan is left alone
        if info is None or _expired(info.get("tags") or {}, now) is None or not safety.is_labagent_group(info.get("tags")):
            row.update(state="skipped", reason="changed since the plan")
            continue
        try:
            lifecycle.destroy_owned(az, item["id"], info["tags"]["name"], log)
            row["state"] = "done"
        except PackError as e:
            row.update(state="failed", error=str(e)[:400])
    env.summary = {s: sum(1 for i in env.items if i["state"] == s) for s in ("done", "skipped", "failed")}
    return env.finalize()
