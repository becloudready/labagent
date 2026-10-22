"""Single entry point used by the CI runner: execute a job document fetched from the orchestrator."""
from __future__ import annotations

from typing import Any, Dict

from .context import Ctx
from .discovery import Scope
from .envelope import Envelope
from . import audit as audit_mod, cleanup as cleanup_mod, iam_ops, sweep

COMMANDS = {"provision", "inventory", "sweep", "reset", "offboard", "audit", "cleanup"}


def run_job(ctx: Ctx, job: Dict[str, Any]) -> Envelope:
    """job = {run_id, command, mode, cohort{id,created_date}, learners[], plan?, approved_hash?, options{}, secrets_pubkey_pem?}"""
    cmd = job.get("command")
    run_id = job.get("run_id")
    if cmd not in COMMANDS:
        return Envelope(command=str(cmd)[:40], run_id=run_id).fail("unknown command")
    cohort = job.get("cohort") or {}
    cid, cdate = cohort.get("id"), cohort.get("created_date")
    learners = job.get("learners") or []
    opts = job.get("options") or {}
    try:
        if cmd == "provision":
            return iam_ops.provision(ctx, cid, cdate, learners, job.get("secrets_pubkey_pem"),
                                     dry_run=job.get("mode") == "dry-run", run_id=run_id,
                                     password_reset_required=opts.get("password_reset_required"))
        if cmd == "reset":
            target = next((l for l in learners if str(l.get("learner_id")) == str(opts.get("learner_id"))), None)
            if not target:
                return Envelope(command="reset", run_id=run_id).fail("learner not in job roster")
            return iam_ops.reset(ctx, cid, target, job.get("secrets_pubkey_pem"), run_id=run_id,
                                 password_reset_required=opts.get("password_reset_required"))
        if cmd == "offboard":
            return iam_ops.offboard(ctx, cid, cdate, learners, force=bool(opts.get("force")), run_id=run_id)
        if cmd == "audit":
            return cleanup_mod.audit(ctx, job.get("ruleset"), opts.get("regions"), run_id=run_id)
        if cmd == "cleanup":
            if job.get("mode") == "apply":
                if not job.get("plan"):
                    return Envelope(command="cleanup", mode="apply", run_id=run_id).fail("apply requires a stored plan")
                env = sweep.sweep_apply(ctx, job["plan"], job.get("approved_hash"), run_id=run_id)
                env.command = "cleanup"
                return env
            if not job.get("ruleset"):
                return Envelope(command="cleanup", mode="plan", run_id=run_id).fail("cleanup requires a ruleset")
            return cleanup_mod.cleanup_plan(ctx, job["ruleset"], opts.get("regions"), run_id=run_id)
        scope = Scope(cohort_id=cid, cohort_date=cdate, all_cohorts=bool(opts.get("all_cohorts")))
        if cmd == "inventory":
            return sweep.inventory(ctx, scope, learners, run_id=run_id)
        if job.get("mode") == "apply":
            if not job.get("plan"):
                return Envelope(command="sweep", mode="apply", run_id=run_id).fail("apply requires a stored plan")
            return sweep.sweep_apply(ctx, job["plan"], job.get("approved_hash"), run_id=run_id)
        return sweep.sweep_plan(ctx, scope, learners, only_user=opts.get("only_learner_id"), run_id=run_id)
    except Exception as e:  # never leak a traceback with data into logs; report the class only
        return Envelope(command=cmd, run_id=run_id, cohort=cid).fail(f"{type(e).__name__}: {str(e)[:200]}")
