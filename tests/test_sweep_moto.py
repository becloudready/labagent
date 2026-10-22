import io
import json
import zipfile

import boto3
import pytest

from labagent.discovery import Scope
from labagent import sweep
from labagent.deleters import Result
from tests.conftest import ACCOUNT

TAGS = {"workshop": "full-stack", "autodelete": "true", "date": "12-Jul-2026", "cohort": "c1"}


def make_zip():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("index.py", "def handler(e, c): return 1")
    return b.getvalue()


@pytest.fixture
def lab(ctx):
    """Create tagged resources for two learners, one protected, one other cohort."""
    iam = boto3.client("iam", region_name="us-east-1")
    role = iam.create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    lam = boto3.client("lambda", region_name="us-east-1")
    lam.create_function(FunctionName="student-ann-lee-fn", Runtime="python3.12", Role=role,
                        Handler="index.handler", Code={"ZipFile": make_zip()}, Tags=TAGS)
    lam.create_function(FunctionName="student-bo-kim-fn", Runtime="python3.12", Role=role,
                        Handler="index.handler", Code={"ZipFile": make_zip()}, Tags=TAGS)
    lam.create_function(FunctionName="keep-me", Runtime="python3.12", Role=role,
                        Handler="index.handler", Code={"ZipFile": make_zip()},
                        Tags={**TAGS, "autodelete": "false"})
    lam.create_function(FunctionName="student-zed-fn", Runtime="python3.12", Role=role,
                        Handler="index.handler", Code={"ZipFile": make_zip()},
                        Tags={**TAGS, "cohort": "c2", "date": "01-Jan-2026"})
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="student-ann-lee-site")
    s3.put_bucket_tagging(Bucket="student-ann-lee-site", Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]})
    s3.put_object(Bucket="student-ann-lee-site", Key="a.txt", Body=b"x")
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(TableName="student-bo-kim-tbl", KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                     BillingMode="PAY_PER_REQUEST", Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])
    ec2 = boto3.client("ec2", region_name="us-east-1")
    ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": [
        {"Key": k, "Value": v} for k, v in {**TAGS, "Name": "student-ann-lee-eip"}.items()]}])
    return ctx


LEARNERS = [{"learner_id": "7", "username": "ann-lee@example.com"},
            {"learner_id": "8", "username": "bo-kim@example.com"}]


def test_plan_lists_and_skips(lab):
    env = sweep.sweep_plan(lab, Scope(cohort_id="c1"), LEARNERS)
    by_id = {i["id"]: i for i in env.items}
    assert by_id["student-ann-lee-fn"]["action"] == "delete" and by_id["student-ann-lee-fn"]["user"] == "7"
    assert by_id["keep-me"]["reason"] == "autodelete=false"
    assert by_id["student-zed-fn"]["reason"] == "other-cohort"
    assert env.plan["hash"] and env.summary["to_delete"] >= 5


def test_plan_is_read_only(lab):
    sweep.sweep_plan(lab, Scope(cohort_id="c1"), LEARNERS)
    names = [f["FunctionName"] for f in boto3.client("lambda", region_name="us-east-1").list_functions()["Functions"]]
    assert len(names) == 4


def test_apply_deletes_exactly_plan(lab):
    plan = sweep.sweep_plan(lab, Scope(cohort_id="c1"), LEARNERS).plan
    env = sweep.sweep_apply(lab, plan, plan["hash"])
    assert env.status == "ok", env.to_dict()
    lam = boto3.client("lambda", region_name="us-east-1")
    left = sorted(f["FunctionName"] for f in lam.list_functions()["Functions"])
    assert left == ["keep-me", "student-zed-fn"]
    assert boto3.client("s3", region_name="us-east-1").list_buckets()["Buckets"] == []
    assert boto3.client("dynamodb", region_name="us-east-1").list_tables()["TableNames"] == []
    assert boto3.client("ec2", region_name="us-east-1").describe_addresses()["Addresses"] == []


def test_apply_ignores_new_resources_created_after_plan(lab):
    plan = sweep.sweep_plan(lab, Scope(cohort_id="c1"), LEARNERS).plan
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="created-after-plan")
    sweep.sweep_apply(lab, plan, plan["hash"])
    names = [b["Name"] for b in boto3.client("s3", region_name="us-east-1").list_buckets()["Buckets"]]
    assert names == ["created-after-plan"]


def test_apply_rejects_tampered_plan(lab):
    plan = sweep.sweep_plan(lab, Scope(cohort_id="c1"), LEARNERS).plan
    plan["items"].append({"arn": "arn:aws:lambda:us-east-1:1:function:keep-me", "action": "delete",
                          "service": "lambda", "type": "function", "id": "keep-me", "region": "us-east-1", "name": "keep-me"})
    env = sweep.sweep_apply(lab, plan)
    assert env.status == "failed" and "hash" in env.error
    assert len(boto3.client("lambda", region_name="us-east-1").list_functions()["Functions"]) == 4


def test_apply_rejects_expired_plan(lab, clock):
    plan = sweep.sweep_plan(lab, Scope(cohort_id="c1"), LEARNERS).plan
    clock.t += lab.cfg.plan_max_age_s + 1
    assert sweep.sweep_apply(lab, plan).status == "failed"


def test_only_user_filter(lab):
    env = sweep.sweep_plan(lab, Scope(cohort_id="c1"), LEARNERS, only_user="7")
    deletes = {i["id"] for i in env.items if i["action"] == "delete"}
    assert "student-ann-lee-fn" in deletes and "student-bo-kim-fn" not in deletes


def test_inventory_reports_unattributed_and_per_learner(lab):
    env = sweep.inventory(lab, Scope(cohort_id="c1"), LEARNERS)
    assert env.summary["per_learner"]["7"] >= 2
    assert env.status == "ok"


def test_account_guard(ctx, cfg):
    cfg.allowed_account_ids = ["999999999999"]
    with pytest.raises(Exception):
        sweep.sweep_plan(ctx, Scope(cohort_id="c1"), [])


def test_empty_allowlist_refuses(ctx, cfg):
    cfg.allowed_account_ids = []
    with pytest.raises(Exception):
        sweep.sweep_plan(ctx, Scope(cohort_id="c1"), [])


def test_throttle_waits_then_pending(ctx, clock, monkeypatch):
    from labagent import deleters
    calls = {"n": 0}

    def always_throttled(c, r):
        calls["n"] += 1
        return Result("throttled", "Throttling", 31)
    monkeypatch.setitem(deleters.REGISTRY, ("lambda", "function"), (always_throttled, 30))
    plan = {"id": "p", "cohort": "c1", "created_at": clock.t, "items": [
        {"arn": "arn:aws:lambda:us-east-1:1:function:f", "action": "delete", "service": "lambda",
         "type": "function", "id": "f", "region": "us-east-1", "name": "f"}]}
    from labagent.plan import build_plan
    plan = build_plan("c1", ACCOUNT, plan["items"], clock.t)
    env = sweep.sweep_apply(ctx, plan)
    assert env.pending and env.status == "partial"
    assert clock.slept and calls["n"] > 1
