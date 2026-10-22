"""Sweep: plan (read-only) then apply (delete exactly the plan). Also inventory."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .context import Ctx, log
from .deleters import REGISTRY, supported_types
from .discovery import Resource, Scope, attribute, classify, discover, slugs_from_roster
from .envelope import Envelope
from .plan import PlanError, build_plan, verify_plan


def _item(r: Resource, action: str, reason: Optional[str], owner: Optional[str]) -> Dict[str, Any]:
    return {"arn": r.arn, "service": r.service, "type": r.rtype, "id": r.rid, "region": r.region,
            "name": r.name, "user": owner, "action": action, "reason": reason, "state": "planned"}


def _classified(ctx: Ctx, scope: Scope, learners: List[Dict[str, str]], only_user: Optional[str]):
    scope.validate()
    slugs = slugs_from_roster(learners)
    out = []
    for r in discover(ctx):
        action, reason = classify(r, ctx.cfg, scope, supported_types())
        owner = attribute(r, ctx.cfg, slugs) if slugs else None
        if only_user is not None and action == "delete" and owner != only_user:
            action, reason = "skip", "not-owned-by-user" if owner else "unattributed"
        out.append(_item(r, action, reason, owner))
    return out


def inventory(ctx: Ctx, scope: Scope, learners: List[Dict[str, str]], run_id: Optional[str] = None) -> Envelope:
    env = Envelope(command="inventory", mode="read", run_id=run_id, cohort=scope.cohort_id or scope.cohort_date)
    items = _classified(ctx, scope, learners, None)
    for i in items:
        i["state"] = "listed"
    env.items = items
    env.unattributed = [i for i in items if i["action"] == "delete" and not i["user"]]
    per_user: Dict[str, int] = {}
    for i in items:
        if i["user"]:
            per_user[i["user"]] = per_user.get(i["user"], 0) + 1
    env.summary = {"total": len(items), "in_scope": sum(1 for i in items if i["action"] == "delete"),
                   "unattributed": len(env.unattributed), "per_learner": per_user}
    unsupported = sorted({i["reason"] for i in items if (i["reason"] or "").startswith("unsupported:")})
    env.warnings = [f"{u} found but no deleter exists" for u in unsupported]
    log(f"inventory: {len(items)} resources, {len(env.unattributed)} unattributed")
    return env.finalize()


def sweep_plan(ctx: Ctx, scope: Scope, learners: List[Dict[str, str]], only_user: Optional[str] = None,
               run_id: Optional[str] = None) -> Envelope:
    ctx.guard_account()
    cohort = scope.cohort_id or scope.cohort_date or "ALL"
    env = Envelope(command="sweep", mode="plan", run_id=run_id, cohort=cohort)
    items = _classified(ctx, scope, learners, only_user)
    env.items = items
    env.plan = build_plan(cohort, ctx.account_id, items, ctx.clock())
    env.unattributed = [i for i in items if i["action"] == "delete" and not i["user"]]
    n = sum(1 for i in items if i["action"] == "delete")
    env.summary = {"to_delete": n, "skipped": len(items) - n}
    unsupported = sorted({i["reason"] for i in items if (i["reason"] or "").startswith("unsupported:")})
    env.warnings = [f"{u} found but no deleter exists" for u in unsupported]
    log(f"plan {env.plan['id']}: delete {n}, skip {len(items) - n}")
    return env


def sweep_apply(ctx: Ctx, plan: Dict[str, Any], approved_hash: Optional[str] = None,
                run_id: Optional[str] = None) -> Envelope:
    ctx.guard_account()
    env = Envelope(command="sweep", mode="apply", run_id=run_id, cohort=plan.get("cohort"))
    try:
        verify_plan(plan, ctx.account_id, ctx.clock(), ctx.cfg.plan_max_age_s, approved_hash)
    except PlanError as e:
        return env.fail(str(e))
    env.plan = {"id": plan["id"], "hash": plan["hash"], "created_at": plan["created_at"]}
    work = [i for i in plan["items"] if i["action"] == "delete"]
    work.sort(key=lambda i: REGISTRY.get((i["service"], i["type"]), (None, 999))[1])
    started = ctx.clock()
    done = []
    for item in work:
        deleter = REGISTRY.get((item["service"], item["type"]))
        out = dict(item)
        if deleter is None:
            out.update(state="failed", reason="no deleter")
            done.append(out)
            continue
        res = Resource(arn=item["arn"], service=item["service"], rtype=item["type"], rid=item["id"],
                       region=item["region"], name=item["name"], tags={})
        r = deleter[0](ctx, res)
        # Throttled (e.g. REST API deletes: 1 per 30 s): wait inside the time budget, then retry once.
        while r.state == "throttled" and (ctx.clock() - started) + (r.retry_after_s or 30) < ctx.cfg.apply_time_budget_s:
            ctx.sleep(r.retry_after_s or 30)
            r = deleter[0](ctx, res)
        if r.state == "throttled":
            r.state = "pending"
        out.update(state=r.state, reason=r.reason)
        if r.state == "pending":
            env.pending.append({"arn": item["arn"], "type": item["type"], "id": item["id"],
                                "reason": r.reason, "retry_after_s": r.retry_after_s or 300})
        done.append(out)
    env.items = done
    counts: Dict[str, int] = {}
    for d in done:
        counts[d["state"]] = counts.get(d["state"], 0) + 1
    env.summary = counts
    log(f"apply {plan['id']}: {counts}")
    return env.finalize()
