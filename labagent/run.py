"""Single entry point used by the CI runner: execute a job document fetched from the orchestrator."""
from __future__ import annotations

import re
from typing import Any, Dict

from .context import Ctx
from .discovery import Scope
from .envelope import Envelope
from . import cleanup as cleanup_mod, iam_ops, sweep

PACK_COMMANDS = {"launch", "destroy"}
COMMANDS = {"provision", "inventory", "sweep", "reset", "offboard", "audit", "cleanup"} | PACK_COMMANDS
# Parameters a remote job may not set: they point at the runner's own disk or identity, not at the lab.
JOB_PARAM_DENY = {"image", "profile"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def run_pack_job(job: Dict[str, Any]) -> Envelope:
    """job = {run_id, command: launch|destroy, pack, name, params{}, mode?: dry-run, secrets_pubkey_pem?}. Needs no AWS context."""
    from .packs import registry
    from .packs.base import run_pack
    cmd, run_id = job["command"], job.get("run_id")
    env = Envelope(command=cmd, run_id=run_id)
    name, params = str(job.get("name") or ""), job.get("params") or {}
    if not NAME_RE.match(name):
        return env.fail("name must be lowercase letters, digits and hyphens (1 to 31 characters)")
    if not isinstance(params, dict) or JOB_PARAM_DENY & set(params):
        return env.fail("params must be an object and may not set: " + ", ".join(sorted(JOB_PARAM_DENY)))
    try:
        pack = registry.resolve(str(job.get("pack") or ""))
    except (KeyError, ValueError) as e:
        return env.fail(str(e.args[0] if e.args else e)[:200])
    out = run_pack(pack, cmd, name, params, dry_run=job.get("mode") == "dry-run", pubkey_pem=job.get("secrets_pubkey_pem"))
    out.run_id = run_id
    return out


def run_job(ctx: Ctx, job: Dict[str, Any]) -> Envelope:
    """job = {run_id, command, mode, cohort{id,created_date}, learners[], plan?, approved_hash?, options{}, secrets_pubkey_pem?}"""
    cmd = job.get("command")
    run_id = job.get("run_id")
    if cmd not in COMMANDS:
        return Envelope(command=str(cmd)[:40], run_id=run_id).fail("unknown command")
    if cmd in PACK_COMMANDS:
        try:
            return run_pack_job(job)
        except Exception as e:
            return Envelope(command=cmd, run_id=run_id).fail(f"{type(e).__name__}: {str(e)[:200]}")
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
