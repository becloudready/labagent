from datetime import datetime, timedelta, timezone

import boto3
import pytest

from labagent import audit, cleanup, sweep
from labagent.config import Config
from labagent.discovery import Resource
from labagent.rules import RulesetError, _parse_dt, decide, matches, validate
from labagent.run import run_job

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
CFG = Config()


def res(service="ec2", rtype="key-pair", name="k", tags=None, region="us-east-1", **attrs):
    return Resource(arn=f"labagent:{service}/{rtype}/{region}/{name}", service=service, rtype=rtype, rid=name,
                    region=region, name=name, tags=tags or {}, attrs=attrs)


def rs(*rules):
    return {"version": 1, "name": "t", "rules": list(rules)}


def test_parse_dt_formats():
    assert _parse_dt("2026-07-01 22:58:38+00:00").year == 2026
    assert _parse_dt("2026-09-18 15:17:08-04:00").hour == 15
    assert _parse_dt("2026-07-30T16:34:09.000+0000").minute == 34
    assert _parse_dt("1788000000000").year >= 2026
    assert _parse_dt(None) is None and _parse_dt("None") is None


@pytest.mark.parametrize("bad", [
    {"version": 2, "rules": []},
    rs({"id": "a", "action": "delete", "match": {}}),
    rs({"id": "a", "action": "delete", "match": {"name": ["x*"]}}),            # delete must name a type
    rs({"id": "a", "action": "nuke", "match": {"type": ["iam/user"]}}),
    rs({"id": "a", "action": "keep", "match": {"typo": 1}}),
    rs({"id": "a", "action": "keep", "match": {"type": ["x/y"]}}, {"id": "a", "action": "keep", "match": {"type": ["x/y"]}}),
])
def test_invalid_rulesets_rejected(bad):
    with pytest.raises(RulesetError):
        validate(bad)


def test_default_is_review_never_delete():
    assert decide(res(), rs(), CFG, NOW).action == "review"


def test_keep_beats_delete_regardless_of_order():
    r = rs({"id": "del", "action": "delete", "match": {"type": ["ec2/key-pair"]}},
           {"id": "keep", "action": "keep", "match": {"type": ["ec2/key-pair"], "name": ["instructor-*"]}})
    assert decide(res(name="instructor-key"), r, CFG, NOW).action == "keep"
    assert decide(res(name="student-key"), r, CFG, NOW).action == "delete"


def test_disabled_rule_has_no_effect():
    r = rs({"id": "del", "action": "delete", "enabled": False, "match": {"type": ["ec2/key-pair"]}})
    assert decide(res(), r, CFG, NOW).action == "review"


def test_builtin_protected_prefix_beats_delete():
    r = rs({"id": "del", "action": "delete", "match": {"type": ["s3/bucket"]}})
    assert decide(res("s3", "bucket", "aws-cloudtrail-logs-xyz"), r, CFG, NOW).action == "keep"


def test_match_operators():
    old = (NOW - timedelta(days=100)).isoformat()
    new = (NOW - timedelta(days=2)).isoformat()
    assert matches(res(created=old), {"older_than_days": 30}, NOW)
    assert not matches(res(created=new), {"older_than_days": 30}, NOW)
    assert not matches(res(), {"older_than_days": 30}, NOW)                 # unknown age never matches
    assert matches(res("iam", "user", "a@x", created=old), {"unused_days": 30}, NOW)           # never used, old
    assert not matches(res("iam", "user", "a@x", created=new), {"unused_days": 30}, NOW)
    assert not matches(res("iam", "user", "a@x", created=old, last_used=new), {"unused_days": 30}, NOW)
    assert matches(res(tags={"Env": "Prod"}), {"tags": {"env": "p*"}}, NOW)
    assert matches(res(tags={}), {"tags_absent": ["workshop"]}, NOW)
    assert not matches(res(tags={"Workshop": "x"}), {"tags_absent": ["workshop"]}, NOW)
    assert matches(res("iam", "policy", "p", attachments=0), {"attrs": {"attachments": 0}}, NOW)
    assert matches(res(name="bob@example.com"), {"name_regex": "@example\\.com$"}, NOW)
    assert matches(res(region="us-west-2"), {"region": ["us-west-*"]}, NOW)


# --- moto: discovery, deleters, plan/apply ------------------------------------------------------

TRUST = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'


@pytest.fixture
def world(ctx):
    iam = boto3.client("iam")
    for u in ("ann@example.com", "bo@example.com", "admin-user"):
        iam.create_user(UserName=u)
    iam.create_login_profile(UserName="ann@example.com", Password="Abcdefgh1!xyz")
    iam.create_access_key(UserName="ann@example.com")
    iam.create_group(GroupName="students")
    iam.add_user_to_group(GroupName="students", UserName="ann@example.com")
    pol = iam.create_policy(PolicyName="AWSLambdaBasicExecutionRole-111", PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"logs:*","Resource":"*"}]}')["Policy"]["Arn"]
    iam.create_policy(PolicyName="orphan-policy", PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"logs:*","Resource":"*"}]}')
    iam.create_role(RoleName="student-lambda-role", AssumeRolePolicyDocument=TRUST)
    iam.attach_role_policy(RoleName="student-lambda-role", PolicyArn=pol)
    iam.put_role_policy(RoleName="student-lambda-role", PolicyName="inline", PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:*","Resource":"*"}]}')
    ec2 = boto3.client("ec2", region_name="us-east-1")
    ec2.create_key_pair(KeyName="student-key")
    ec2.create_key_pair(KeyName="instructor-ec2-key")
    boto3.client("sns", region_name="us-east-1").create_topic(Name="labs-student1")
    boto3.client("sqs", region_name="us-east-1").create_queue(QueueName="student-q")
    return ctx


RULES = rs(
    {"id": "keep-admin", "action": "keep", "match": {"type": ["iam/user"], "name": ["admin-*"]}},
    {"id": "keep-instructor-key", "action": "keep", "match": {"type": ["ec2/key-pair"], "name": ["instructor-*"]}},
    {"id": "students", "action": "delete", "match": {"type": ["iam/user"], "name_regex": "@example\\.com$"}},
    {"id": "lambda-policies", "action": "delete", "match": {"type": ["iam/policy"], "name": ["AWSLambdaBasicExecutionRole-*"]}},
    {"id": "student-role", "action": "delete", "match": {"type": ["iam/role"], "name": ["student-*"]}},
    {"id": "group", "action": "delete", "match": {"type": ["iam/group"], "name": ["students"]}},
    {"id": "keys", "action": "delete", "match": {"type": ["ec2/key-pair"]}},
    {"id": "sns", "action": "delete", "match": {"type": ["sns/topic"], "name": ["labs-*"]}},
    {"id": "sqs", "action": "delete", "match": {"type": ["sqs/queue"], "name": ["student-*"]}},
)


def test_discover_all_finds_iam_ec2_sns(world):
    found, errors = audit.discover_all(world)
    kinds = {(r.service, r.rtype) for r in found}
    assert {("iam", "user"), ("iam", "role"), ("iam", "group"), ("iam", "policy"), ("ec2", "key-pair"),
            ("sns", "topic"), ("sqs", "queue")} <= kinds
    assert not any("iam" in e for e in errors)


def test_audit_is_read_only_and_reports_review(world):
    env = cleanup.audit(world, RULES)
    by = {i["id"]: i["action"] for i in env.items}
    assert by["admin-user"] == "keep" and by["ann@example.com"] == "delete"
    assert by["orphan-policy"] == "review"                      # unmatched: never deleted
    assert len(boto3.client("iam").list_users()["Users"]) == 3


def test_cleanup_plan_then_apply_deletes_only_matches_in_dependency_order(world):
    plan_env = cleanup.cleanup_plan(world, RULES)
    assert plan_env.status == "ok" and plan_env.plan["hash"]
    env = sweep.sweep_apply(world, plan_env.plan, plan_env.plan["hash"])
    assert env.status == "ok", env.to_dict()
    iam = boto3.client("iam")
    assert [u["UserName"] for u in iam.list_users()["Users"]] == ["admin-user"]
    assert iam.list_groups()["Groups"] == [] and iam.list_roles()["Roles"] == []
    assert [p["PolicyName"] for p in iam.list_policies(Scope="Local")["Policies"]] == ["orphan-policy"]
    ec2 = boto3.client("ec2", region_name="us-east-1")
    assert [k["KeyName"] for k in ec2.describe_key_pairs()["KeyPairs"]] == ["instructor-ec2-key"]
    assert boto3.client("sns", region_name="us-east-1").list_topics()["Topics"] == []
    assert "QueueUrls" not in boto3.client("sqs", region_name="us-east-1").list_queues()


def test_apply_is_idempotent(world):
    p = cleanup.cleanup_plan(world, RULES).plan
    sweep.sweep_apply(world, p, p["hash"])
    again = sweep.sweep_apply(world, p, p["hash"])
    assert again.status == "ok" and {i["state"] for i in again.items} <= {"gone", "deleted"}


def test_delete_rule_for_type_without_deleter_becomes_review(world, monkeypatch):
    nat = res("ec2", "nat-gateway", "nat-1")
    monkeypatch.setattr(cleanup, "discover_all", lambda ctx, regions=None: ([nat], []))
    env = cleanup.audit(world, rs({"id": "nat", "action": "delete", "match": {"type": ["ec2/nat-gateway"]}}))
    assert env.items[0]["action"] == "review" and "no deleter" in env.items[0]["reason"]


def test_run_job_audit_and_cleanup_commands(world):
    a = run_job(world, {"command": "audit", "ruleset": RULES, "run_id": "r1"})
    assert a.status == "ok" and a.summary["by_action"]["delete"] >= 5
    p = run_job(world, {"command": "cleanup", "mode": "plan", "ruleset": RULES})
    assert p.plan
    assert run_job(world, {"command": "cleanup", "mode": "plan"}).status == "failed"            # no ruleset
    assert run_job(world, {"command": "cleanup", "mode": "apply"}).status == "failed"           # no plan
    bad = run_job(world, {"command": "cleanup", "mode": "plan", "ruleset": rs({"id": "x", "action": "delete", "match": {}})})
    assert bad.status == "failed" and "ruleset invalid" in bad.error


def test_cleanup_respects_account_guard(world):
    world.cfg.allowed_account_ids = ["999999999999"]
    with pytest.raises(Exception):
        cleanup.cleanup_plan(world, RULES)


def test_tag_sweep_never_considers_iam():
    from labagent.deleters import supported_types
    assert ("iam", "user") not in supported_types() and ("cloudformation", "stack") not in supported_types()


def _vpc_world(region="us-east-1"):
    ec2 = boto3.client("ec2", region_name=region)
    vpc = ec2.create_vpc(CidrBlock="10.8.0.0/16")["Vpc"]["VpcId"]
    sn = ec2.create_subnet(VpcId=vpc, CidrBlock="10.8.1.0/24")["Subnet"]["SubnetId"]
    ig = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=ig, VpcId=vpc)
    rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    ec2.associate_route_table(RouteTableId=rt, SubnetId=sn)
    a = ec2.create_security_group(GroupName="sg-a", Description="a", VpcId=vpc)["GroupId"]
    b = ec2.create_security_group(GroupName="sg-b", Description="b", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=a, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "UserIdGroupPairs": [{"GroupId": b}]}])
    ec2.authorize_security_group_ingress(GroupId=b, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "UserIdGroupPairs": [{"GroupId": a}]}])
    return ec2, vpc, sn


def test_vpc_deleter_removes_vpc_and_dependents_including_cross_referencing_groups(world):
    from labagent.deleters import del_vpc
    ec2, vpc, _ = _vpc_world()
    r = Resource("labagent:ec2/vpc/us-east-1/x", "ec2", "vpc", vpc, "us-east-1", "x")
    assert del_vpc(world, r).state == "deleted"
    assert vpc not in [v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"]]
    assert del_vpc(world, r).state == "gone"                                    # idempotent


def test_vpc_deleter_refuses_while_a_network_interface_is_in_use(world):
    from labagent.deleters import del_vpc
    ec2, vpc, sn = _vpc_world()
    ec2.create_network_interface(SubnetId=sn, Description="AWS created network interface for directory d-123")
    out = del_vpc(world, Resource("a", "ec2", "vpc", vpc, "us-east-1", "x"))
    assert out.state == "failed" and "network interface" in out.reason
    assert vpc in [v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"]]            # untouched


def test_vpc_deleter_refuses_while_an_instance_runs(world):
    from labagent.deleters import del_vpc
    ec2, vpc, sn = _vpc_world()
    img = ec2.describe_images()["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=img, MinCount=1, MaxCount=1, SubnetId=sn)
    out = del_vpc(world, Resource("a", "ec2", "vpc", vpc, "us-east-1", "x"))
    assert out.state == "failed" and "EC2 instance" in out.reason


def test_default_vpc_is_review_unless_a_rule_names_it(world):
    env = cleanup.audit(world, rs({"id": "k", "action": "keep", "match": {"type": ["iam/user"], "name": ["x"]}}))
    defaults = [i for i in env.items if i["type"] == "vpc"]
    assert defaults and all(i["action"] == "review" for i in defaults)


def test_broad_vpc_rule_never_deletes_a_default_vpc():
    default = res("ec2", "vpc", "vpc-d", is_default=True)
    custom = res("ec2", "vpc", "vpc-c", is_default=False)
    broad = rs({"id": "vpcs", "action": "delete", "match": {"type": ["ec2/vpc"]}})
    assert decide(default, broad, CFG, NOW).action == "review"
    assert decide(custom, broad, CFG, NOW).action == "delete"
    explicit = rs({"id": "dv", "action": "delete", "match": {"type": ["ec2/vpc"], "attrs": {"is_default": True}}})
    assert decide(default, explicit, CFG, NOW).action == "delete"


class CallLog:
    """Records the order of IAM calls the deleter makes."""

    def __init__(self, real):
        self._real, self.calls = real, []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **kw):
            self.calls.append((name, kw.get("Status"), kw.get("AccessKeyId") or kw.get("UserName")))
            return attr(*a, **kw)
        return wrapped

    def list_service_specific_credentials(self, **kw):
        return {"ServiceSpecificCredentials": []}


def test_iam_user_deletion_deactivates_then_deletes_keys_before_deleting_the_user(world):
    from labagent.deleters import del_iam_user
    iam = boto3.client("iam")
    iam.create_user(UserName="keyed@example.com")
    k1 = iam.create_access_key(UserName="keyed@example.com")["AccessKey"]["AccessKeyId"]
    k2 = iam.create_access_key(UserName="keyed@example.com")["AccessKey"]["AccessKeyId"]
    iam.update_access_key(UserName="keyed@example.com", AccessKeyId=k2, Status="Inactive")   # already inactive
    iam.create_login_profile(UserName="keyed@example.com", Password="Abcdefgh1!xyz")
    log = CallLog(world.client("iam"))
    world._clients[("iam", world.cfg.region)] = log
    out = del_iam_user(world, Resource("a", "iam", "user", "keyed@example.com", "global", "keyed@example.com"))
    assert out.state == "deleted"
    names = [c[0] for c in log.calls]
    # k1 was active: it is deactivated first, then deleted. k2 was already inactive: deleted without an update.
    deact = [i for i, c in enumerate(log.calls) if c[0] == "update_access_key" and c[1] == "Inactive" and c[2] == k1]
    dele = {c[2]: i for i, c in enumerate(log.calls) if c[0] == "delete_access_key"}
    assert deact and deact[0] < dele[k1] and k2 in dele
    assert not [c for c in log.calls if c[0] == "update_access_key" and c[2] == k2]
    assert max(dele.values()) < names.index("delete_login_profile") < names.index("delete_user")
    assert "keyed@example.com" not in [u["UserName"] for u in iam.list_users()["Users"]]


def test_iam_user_deletion_is_idempotent(world):
    from labagent.deleters import del_iam_user
    r = Resource("a", "iam", "user", "ghost@example.com", "global", "ghost")
    assert del_iam_user(world, r).state == "gone"


def test_new_deleters_glue_athena_alarms_dashboards_layers(world):
    import zipfile, io
    from labagent.deleters import (del_alarm, del_athena_workgroup, del_dashboard, del_glue_database, del_glue_job, del_layer)
    g = boto3.client("glue", region_name="us-east-1")
    g.create_database(DatabaseInput={"Name": "spectrum_db_student1"})
    g.create_job(Name="etl", Role="arn:aws:iam::123456789012:role/r", Command={"Name": "glueetl", "ScriptLocation": "s3://b/s.py"})
    boto3.client("athena", region_name="us-east-1").create_work_group(Name="wg1", Configuration={"ResultConfiguration": {"OutputLocation": "s3://b/"}})
    cw = boto3.client("cloudwatch", region_name="us-east-1")
    cw.put_metric_alarm(AlarmName="a1", MetricName="m", Namespace="n", Statistic="Sum", Period=60, EvaluationPeriods=1,
                        Threshold=1, ComparisonOperator="GreaterThanThreshold")
    cw.put_dashboard(DashboardName="d1", DashboardBody='{"widgets":[]}')
    lam = boto3.client("lambda", region_name="us-east-1")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("x.py", "x=1")
    for _ in range(2):
        lam.publish_layer_version(LayerName="deps", Content={"ZipFile": b.getvalue()}, CompatibleRuntimes=["python3.12"])
    R = lambda svc, t, rid: Resource("a", svc, t, rid, "us-east-1", rid)
    for fn, res_ in ((del_glue_database, R("glue", "database", "spectrum_db_student1")), (del_glue_job, R("glue", "job", "etl")),
                     (del_athena_workgroup, R("athena", "workgroup", "wg1")), (del_alarm, R("cloudwatch", "alarm", "a1")),
                     (del_dashboard, R("cloudwatch", "dashboard", "d1")), (del_layer, R("lambda", "layer", "deps"))):
        assert fn(world, res_).state == "deleted", fn.__name__
    assert g.get_databases()["DatabaseList"] == [] and g.get_jobs()["Jobs"] == []
    assert cw.describe_alarms()["MetricAlarms"] == [] and cw.list_dashboards()["DashboardEntries"] == []
    assert lam.list_layers()["Layers"] == []
    assert del_layer(world, R("lambda", "layer", "deps")).state == "gone"
    assert del_athena_workgroup(world, R("athena", "workgroup", "wg1")).state in ("gone", "deleted")   # moto is lenient; real Athena returns InvalidRequest, handled


def test_discover_all_finds_new_types(world):
    boto3.client("glue", region_name="us-east-1").create_database(DatabaseInput={"Name": "db1"})
    boto3.client("cloudwatch", region_name="us-east-1").put_dashboard(DashboardName="d1", DashboardBody='{"widgets":[]}')
    kinds = {(r.service, r.rtype) for r in audit.discover_all(world)[0]}
    assert {("glue", "database"), ("cloudwatch", "dashboard")} <= kinds


def test_security_group_deletion_first_removes_its_own_cross_group_rules(world):
    """moto does not enforce DependencyViolation, so assert the order of calls: revoke own references, then delete."""
    from labagent.deleters import del_sg
    ec2 = boto3.client("ec2", region_name="us-east-1")
    vpc = ec2.create_vpc(CidrBlock="10.7.0.0/16")["Vpc"]["VpcId"]
    a = ec2.create_security_group(GroupName="a", Description="a", VpcId=vpc)["GroupId"]
    b = ec2.create_security_group(GroupName="b", Description="b", VpcId=vpc)["GroupId"]
    for x, y in ((a, b), (b, a)):
        ec2.authorize_security_group_ingress(GroupId=x, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [{"GroupId": y}]}])
    log = CallLog(world.client("ec2"))
    world._clients[("ec2", world.cfg.region)] = log
    assert del_sg(world, Resource("a", "ec2", "security-group", a, "us-east-1", a)).state == "deleted"
    names = [c[0] for c in log.calls]
    assert names.index("revoke_security_group_ingress") < names.index("delete_security_group")
    assert ec2.describe_security_groups(GroupIds=[b])["SecurityGroups"][0]["IpPermissions"][0]["UserIdGroupPairs"]  # peer B untouched
