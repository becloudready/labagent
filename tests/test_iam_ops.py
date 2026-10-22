import base64
import json
import re

import boto3
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from labagent import iam_ops
from labagent.run import run_job
from labagent.tf import TerraformError

LEARNERS = [{"learner_id": "7", "username": "ann-lee@example.com"},
            {"learner_id": "8", "username": "bo-kim@example.com", "active": "false"}]


class FakeTF:
    """Stands in for Terraform: creates/removes the IAM users the real module would manage."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def plan(self, cohort, variables):
        self.calls.append(("plan", cohort, variables))
        iam = boto3.client("iam")
        have = {u["UserName"] for u in iam.list_users()["Users"]}
        new = [k for k, v in variables["learners"].items() if v["username"] not in have]
        return {"create": len(new), "update": 0, "delete": 0, "users_to_create": sorted(new)}

    def apply(self, cohort, variables):
        self.calls.append(("apply", cohort, variables))
        if self.fail:
            raise TerraformError(self.fail)
        iam = boto3.client("iam")
        for v in variables["learners"].values():
            try:
                iam.create_user(UserName=v["username"])
            except iam.exceptions.EntityAlreadyExistsException:
                pass
        return {"console_url": "https://console.example.com", "region": "us-east-1",
                "lambda_role_arn": "arn:aws:iam::123456789012:role/labs-x-lambda-exec", "group_name": "labs-x-students",
                "users": {k: v["username"] for k, v in variables["learners"].items()}}

    def destroy(self, cohort, variables):
        self.calls.append(("destroy", cohort, variables))
        iam = boto3.client("iam")
        for v in variables["learners"].values():
            try:
                try:
                    iam.delete_login_profile(UserName=v["username"])
                except iam.exceptions.NoSuchEntityException:
                    pass
                iam.delete_user(UserName=v["username"])
            except iam.exceptions.NoSuchEntityException:
                pass


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pem


def decrypt(priv, b64):
    return priv.decrypt(base64.b64decode(b64), padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).decode()


def test_provision_applies_terraform_then_issues_encrypted_password(ctx, keypair):
    priv, pem = keypair
    tf = FakeTF()
    env = iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=tf)
    assert env.status == "ok", env.to_dict()
    assert [c[0] for c in tf.calls] == ["apply"]
    assert list(tf.calls[0][2]["learners"]) == ["7"]              # inactive learner never reaches Terraform
    assert [s["user"] for s in env.secrets] == ["7"]
    pw = decrypt(priv, env.secrets[0]["ciphertext"])
    assert len(pw) >= 12 and re.search(r"[A-Z]", pw) and re.search(r"\d", pw)
    assert pw not in json.dumps(env.to_dict())
    assert boto3.client("iam").get_login_profile(UserName="ann-lee@example.com")
    assert env.summary["password_reset_required"] is True
    assert env.summary["lambda_role_arn"].endswith("lambda-exec")


@pytest.mark.parametrize("flag", [True, False])
def test_password_reset_flag_is_passed_to_iam(ctx, keypair, flag):
    """moto does not echo PasswordResetRequired back, so spy on the call."""
    _, pem = keypair
    real, seen = ctx.client("iam"), {}

    class Spy:
        def __getattr__(self, n):
            return getattr(real, n)

        def create_login_profile(self, **kw):
            seen.update(kw)
            return real.create_login_profile(**kw)
    ctx._clients[("iam", ctx.cfg.region)] = Spy()
    env = iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=FakeTF(), password_reset_required=flag)
    assert seen["PasswordResetRequired"] is flag and env.summary["password_reset_required"] is flag


def test_provision_is_idempotent_and_does_not_reissue_password(ctx, keypair):
    _, pem = keypair
    iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=FakeTF())
    again = iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=FakeTF())
    assert again.secrets == [] and again.items[0]["state"] == "exists"


def test_provision_requires_public_key_and_does_not_call_terraform(ctx):
    tf = FakeTF()
    env = iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, None, tf=tf)
    assert env.status == "failed" and "public key" in env.error and tf.calls == []


def test_terraform_failure_is_reported_and_no_password_issued(ctx, keypair):
    _, pem = keypair
    env = iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=FakeTF(fail="terraform apply failed"))
    assert env.status == "failed" and env.secrets == []


def test_provision_dry_run_uses_plan_only(ctx):
    tf = FakeTF()
    env = iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, None, dry_run=True, tf=tf)
    assert env.status == "ok" and env.items[0]["state"] == "planned"
    assert [c[0] for c in tf.calls] == ["plan"] and boto3.client("iam").list_users()["Users"] == []


def test_reset_rotates_password(ctx, keypair):
    priv, pem = keypair
    iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=FakeTF())
    env = iam_ops.reset(ctx, "c1", LEARNERS[0], pem)
    assert env.status == "ok" and decrypt(priv, env.secrets[0]["ciphertext"])


def test_offboard_refuses_while_resources_remain_and_does_not_destroy(ctx, keypair):
    _, pem = keypair
    iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=FakeTF())
    boto3.client("s3").create_bucket(Bucket="left-behind")
    boto3.client("s3").put_bucket_tagging(Bucket="left-behind", Tagging={"TagSet": [
        {"Key": "workshop", "Value": "full-stack"}, {"Key": "cohort", "Value": "c1"}]})
    tf = FakeTF()
    env = iam_ops.offboard(ctx, "c1", None, LEARNERS, tf=tf)
    assert env.status == "failed" and tf.calls == []
    assert boto3.client("iam").list_users()["Users"]


def test_offboard_destroys_when_clean_and_verifies_users_gone(ctx, keypair):
    _, pem = keypair
    iam_ops.provision(ctx, "c1", "12-Jul-2026", LEARNERS, pem, tf=FakeTF())
    tf = FakeTF()
    env = iam_ops.offboard(ctx, "c1", None, LEARNERS, tf=tf)
    assert env.status == "ok" and env.items[0]["state"] == "deleted"
    assert [c[0] for c in tf.calls] == ["destroy"] and boto3.client("iam").list_users()["Users"] == []


def test_run_job_dispatch_and_unknown_command(ctx, keypair, monkeypatch):
    _, pem = keypair
    monkeypatch.setattr(iam_ops, "Terraform", type("T", (), {"__init__": lambda s, cfg: None,
                                                              "variables": staticmethod(lambda *a: {"learners": {}})}))
    job = {"run_id": "r1", "command": "provision", "mode": "dry-run",
           "cohort": {"id": "c1", "created_date": "12-Jul-2026"}, "learners": LEARNERS[:1]}
    env = run_job(ctx, job)
    assert env.run_id == "r1"
    assert run_job(ctx, {"command": "rm -rf"}).status == "failed"


def test_apply_without_plan_is_refused(ctx):
    assert run_job(ctx, {"command": "sweep", "mode": "apply", "cohort": {"id": "c1"}}).status == "failed"


def test_sweep_without_scope_is_refused(ctx):
    assert run_job(ctx, {"command": "sweep", "mode": "plan", "cohort": {}}).status == "failed"
