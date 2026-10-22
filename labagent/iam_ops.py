"""Cohort IAM lifecycle. Terraform owns users, group, policy and role (see tf.py); this module owns passwords.

Passwords are issued with the IAM API after `apply`, encrypted to the orchestrator's public key, and returned
only as ciphertext. They never touch Terraform state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from .context import Ctx, log
from .discovery import Scope
from .envelope import Envelope
from .secrets_box import encrypt, generate_password, load_public_key
from .sweep import inventory
from .tf import Terraform, TerraformError


def _active(learners: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [l for l in learners if str(l.get("active", "true")).lower() != "false"]


def _check_learner(l: Dict[str, Any]) -> None:
    if "@" not in (l.get("username") or ""):
        raise ValueError(f"learner {l.get('learner_id')}: username must be an email-style identifier")


def provision(ctx: Ctx, cohort: str, created_date: str, learners: List[Dict[str, Any]],
              pubkey_pem: Optional[str], dry_run: bool = False, run_id: Optional[str] = None,
              tf: Optional[Terraform] = None, password_reset_required: Optional[bool] = None) -> Envelope:
    ctx.guard_account()
    env = Envelope(command="provision", mode="dry-run" if dry_run else "apply", run_id=run_id, cohort=cohort)
    if not dry_run and not pubkey_pem:
        return env.fail("a secrets public key is required: passwords are never emitted in plaintext")
    tf = tf or Terraform(ctx.cfg)
    active = _active(learners)
    for l in active:
        _check_learner(l)
    variables = Terraform.variables(ctx.cfg, cohort, created_date, active)
    try:
        if dry_run:
            plan = tf.plan(cohort, variables)
            to_create = set(plan["users_to_create"])
            env.items = [{"user": str(l["learner_id"]), "type": "iam-user", "id": l["username"], "action": "create",
                          "state": "planned" if str(l["learner_id"]) in to_create else "exists"} for l in active]
            env.summary = {"terraform": {k: plan[k] for k in ("create", "update", "delete")}}
            return env.finalize()
        outputs = tf.apply(cohort, variables)
    except TerraformError as e:
        return env.fail(str(e))

    reset_required = ctx.cfg.password_reset_required if password_reset_required is None else password_reset_required
    pub, iam = load_public_key(pubkey_pem), ctx.client("iam")
    created = existed = 0
    for l in active:
        lid, name = str(l["learner_id"]), l["username"]
        item = {"user": lid, "type": "iam-user", "id": name, "action": "create"}
        try:
            iam.get_login_profile(UserName=name)
            item["state"] = "exists"
            existed += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
            pw = generate_password(ctx.cfg.password_length)
            iam.create_login_profile(UserName=name, Password=pw, PasswordResetRequired=reset_required)
            env.secrets.append({"user": lid, "kind": "console_password", "ciphertext": encrypt(pub, pw)})
            item["state"] = "created"
            created += 1
        env.items.append(item)
    env.summary = {"created": created, "existed": existed, "console_url": outputs.get("console_url"),
                   "region": outputs.get("region"), "lambda_role_arn": outputs.get("lambda_role_arn"),
                   "group_name": outputs.get("group_name"), "password_reset_required": reset_required}
    log(f"provision: passwords issued {created}, already had one {existed}")
    return env.finalize()


def reset(ctx: Ctx, cohort: str, learner: Dict[str, Any], pubkey_pem: Optional[str],
          run_id: Optional[str] = None, password_reset_required: Optional[bool] = None) -> Envelope:
    """Rotate the console password and remove access keys. Resource reset is `sweep` with only_learner_id."""
    ctx.guard_account()
    env = Envelope(command="reset", mode="apply", run_id=run_id, cohort=cohort)
    if not pubkey_pem:
        return env.fail("a secrets public key is required")
    _check_learner(learner)
    reset_required = ctx.cfg.password_reset_required if password_reset_required is None else password_reset_required
    iam, name = ctx.client("iam"), learner["username"]
    pw = generate_password(ctx.cfg.password_length)
    try:
        try:
            iam.update_login_profile(UserName=name, Password=pw, PasswordResetRequired=reset_required)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
            iam.create_login_profile(UserName=name, Password=pw, PasswordResetRequired=reset_required)
        for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
            iam.delete_access_key(UserName=name, AccessKeyId=k["AccessKeyId"])
    except ClientError as e:
        return env.fail(e.response["Error"]["Code"])
    lid = str(learner["learner_id"])
    env.secrets.append({"user": lid, "kind": "console_password", "ciphertext": encrypt(load_public_key(pubkey_pem), pw)})
    env.items.append({"user": lid, "type": "iam-user", "id": name, "action": "reset", "state": "reset"})
    return env.finalize()


def offboard(ctx: Ctx, cohort: str, created_date: Optional[str], learners: List[Dict[str, Any]],
             force: bool = False, run_id: Optional[str] = None, tf: Optional[Terraform] = None) -> Envelope:
    """terraform destroy for the cohort. Refuses while in-scope lab resources remain, unless force."""
    ctx.guard_account()
    env = Envelope(command="offboard", mode="apply", run_id=run_id, cohort=cohort)
    left = inventory(ctx, Scope(cohort_id=cohort, cohort_date=created_date), learners)
    remaining = [i for i in left.items if i["action"] == "delete"]
    if remaining and not force:
        env.pending = [{"reason": "resources remain: sweep first", "count": len(remaining)}]
        return env.fail(f"{len(remaining)} in-scope resources remain; run sweep first (or force)")
    tf = tf or Terraform(ctx.cfg)
    active = _active(learners)
    try:
        tf.destroy(cohort, Terraform.variables(ctx.cfg, cohort, created_date or "unknown", active))
    except TerraformError as e:
        return env.fail(str(e))
    iam = ctx.client("iam")
    for l in active:
        try:
            iam.get_user(UserName=l["username"])
            state = "failed"          # destroy reported success but the user is still there
        except ClientError as e:
            state = "deleted" if e.response["Error"]["Code"] == "NoSuchEntity" else "failed"
        env.items.append({"user": str(l["learner_id"]), "type": "iam-user", "id": l["username"],
                          "action": "delete", "state": state})
    log(f"offboard: {len(env.items)} users verified")
    return env.finalize()
