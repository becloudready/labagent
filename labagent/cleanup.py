"""Ruleset-driven audit and cleanup of an existing account. Plan, then apply the plan with sweep_apply."""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Optional

from .audit import discover_all
from .context import Ctx, log
from .deleters import REGISTRY, cleanup_types
from .envelope import Envelope
from .plan import build_plan
from .rules import RulesetError, decide, validate


def _decide_all(ctx: Ctx, ruleset: Dict[str, Any], regions=None):
    validate(ruleset)
    resources, errors = discover_all(ctx, regions)
    supported = set(cleanup_types())
    rows = []
    for r in resources:
        d = decide(r, ruleset, ctx.cfg)
        action, reason = d.action, d.reason
        if action == "delete" and (r.service, r.rtype) not in supported:
            action, reason = "review", f"matched rule {d.rule} but labagent has no deleter for {r.service}/{r.rtype}"
        rows.append({"arn": r.arn, "service": r.service, "type": r.rtype, "id": r.rid, "region": r.region,
                     "name": r.name, "user": None, "action": action, "rule": d.rule, "reason": reason,
                     "state": "planned"})
    return rows, errors


def _summary(rows) -> Dict[str, Any]:
    by_action = Counter(r["action"] for r in rows)
    by_type: Dict[str, Dict[str, int]] = {}
    for r in rows:
        by_type.setdefault(f"{r['service']}/{r['type']}", Counter())[r["action"]] += 1
    return {"total": len(rows), "by_action": dict(by_action), "by_type": {k: dict(v) for k, v in sorted(by_type.items())}}


def audit(ctx: Ctx, ruleset: Optional[Dict[str, Any]], regions=None, run_id: Optional[str] = None) -> Envelope:
    """Read-only. Lists everything and what the ruleset would do with it. No plan is created."""
    env = Envelope(command="audit", mode="read", run_id=run_id, cohort=(ruleset or {}).get("name"))
    try:
        rows, errors = _decide_all(ctx, ruleset or {"version": 1, "rules": []}, regions)
    except RulesetError as e:
        return env.fail(f"ruleset invalid: {e}")
    env.items, env.warnings = rows, [f"discovery error: {e}" for e in errors]
    for i in env.items:
        i["state"] = "listed"
    env.summary = _summary(rows)
    log(f"audit: {len(rows)} resources, {env.summary['by_action']}")
    return env.finalize()


def cleanup_plan(ctx: Ctx, ruleset: Dict[str, Any], regions=None, run_id: Optional[str] = None) -> Envelope:
    ctx.guard_account()
    name = ruleset.get("name", "cleanup")
    env = Envelope(command="cleanup", mode="plan", run_id=run_id, cohort=f"cleanup:{name}")
    try:
        rows, errors = _decide_all(ctx, ruleset, regions)
    except RulesetError as e:
        return env.fail(f"ruleset invalid: {e}")
    env.items = rows
    env.warnings = [f"discovery error: {e}" for e in errors]
    env.plan = build_plan(env.cohort, ctx.account_id, rows, ctx.clock())
    env.summary = _summary(rows)
    log(f"cleanup plan {env.plan['id']}: {env.summary['by_action']}")
    return env
