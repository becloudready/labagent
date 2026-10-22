"""Plan integrity: apply deletes exactly what a reviewed plan lists, nothing discovered at apply time."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List


class PlanError(Exception):
    pass


def _canon(cohort: str, account_id: str, items: List[Dict[str, Any]]) -> str:
    core = [{"arn": i["arn"], "action": i["action"]} for i in sorted(items, key=lambda x: x["arn"])]
    return json.dumps({"cohort": cohort, "account": account_id, "items": core}, sort_keys=True, separators=(",", ":"))


def build_plan(cohort: str, account_id: str, items: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "hash": hashlib.sha256(_canon(cohort, account_id, items).encode()).hexdigest(),
        "cohort": cohort,
        "created_at": now,
        "items": items,
    }


def verify_plan(plan: Dict[str, Any], account_id: str, now: float, max_age_s: int, expected_hash: str = None) -> None:
    items = plan.get("items")
    if not isinstance(items, list) or "hash" not in plan:
        raise PlanError("malformed plan")
    want = hashlib.sha256(_canon(plan["cohort"], account_id, items).encode()).hexdigest()
    if want != plan["hash"]:
        raise PlanError("plan hash mismatch (tampered, or created for a different account)")
    if expected_hash and expected_hash != plan["hash"]:
        raise PlanError("plan hash does not match the approved hash")
    if now - float(plan["created_at"]) > max_age_s:
        raise PlanError("plan expired: create a new plan and re-approve")
