import csv
import re

import pytest

from labagent.packs.base import load_state, run_pack
from labagent.packs.rag_ide import ec2 as e
from labagent.packs.rag_ide.pack import RagIde, creds_path


class FakeEc2:
    def __init__(self, ready_after=0, authed=True):
        self.region = self.profile = None
        self.authed, self.ready_after, self.polls = authed, ready_after, 0
        self.runs, self.terminated, self.sg_deleted, self.instances = [], [], False, {}
        self.pauses = []

    def check_auth(self):
        if not self.authed:
            from labagent.packs.base import PackError
            raise PackError("current AWS account is not in allowed_account_ids: refusing to run.")
        return "acct-alpha in us-east-1"

    def security_group(self, cohort): return "sg-123"

    def run(self, cohort, sg, user_data, instance_type, spot):
        iid = f"i-{len(self.runs) + 1:04d}"
        self.runs.append({"sg": sg, "ud": user_data, "type": instance_type, "spot": spot})
        self.instances[iid] = True
        return iid

    def public_ip(self, iid): return "203.0.113.7"
    def alive(self, iid): return self.instances.get(iid, False)
    def find(self, cohort): return [i for i, up in self.instances.items() if up]
    def pause(self, s): self.pauses.append(s)

    def ready(self, host, learner, verify=True):
        self.verify_seen = getattr(self, 'verify_seen', []) + [verify]
        self.polls += 1
        return self.polls > self.ready_after

    def terminate(self, ids):
        self.terminated += ids
        for i in ids: self.instances[i] = False

    def delete_security_group(self, cohort): self.sg_deleted = True


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))


def launch(fake, **params):
    lines = []
    env = run_pack(RagIde(), "launch", "c1", {"platform": "ec2", "learners": "2", **params}, services={"ec2": fake}, log=lines.append)
    return env, "\n".join(lines)


def test_instance_type_scales_with_learners_and_rejects_nonsense():
    assert e.pick_instance_type(1, 4096) == "m6i.large"
    assert e.pick_instance_type(5, 4096) == "m6i.xlarge"
    assert e.pick_instance_type(30, 4096) == "m6i.4xlarge"
    assert e.pick_instance_type(30, 4096, "c6i.4xlarge") == "c6i.4xlarge"
    with pytest.raises(Exception):
        e.pick_instance_type(30, 4096, "big; rm -rf /")
    with pytest.raises(Exception):
        e.pick_instance_type(200, 4096)


def test_user_data_is_pinned_limited_and_self_terminating():
    ud = e.build_user_data({"ana": "pw_ana_12345", "ben": "pw_ben_12345"}, ref=e.PINNED_REF, hours=3, idle_minutes=10, cpu=2, memory_mib=4096)
    assert "shutdown -h +180" in ud and f'test "$(git rev-parse HEAD)" = "{e.PINNED_REF}"' in ud
    assert "--cap-drop ALL" in ud and "no-new-privileges" in ud and "--memory 4096m" in ud and "--pids-limit" in ud and "--cpus 2" in ud
    assert "--idle-timeout-seconds 600" in ud and "handle_path /ana/*" in ud and "reverse_proxy lab-ben:8080" in ud
    assert "shutdown -h now" in ud and "sslip.io" in ud and "169.254.169.254" in ud
    assert "tls internal" not in ud and "tls internal" in e.build_user_data({"a": "pw_a_123456"}, ref=e.PINNED_REF, hours=1, idle_minutes=5, cpu=1, memory_mib=2048, tls="internal")


def test_user_data_rejects_unsafe_values():
    ok = dict(ref=e.PINNED_REF, hours=1, idle_minutes=5, cpu=1, memory_mib=2048)
    for bad in ({"a b": "pw_12345678"}, {"a": "short"}, {"a": "pw_1234; reboot"}, {"A": "pw_12345678"}):
        with pytest.raises(Exception):
            e.build_user_data(bad, **ok)
    with pytest.raises(Exception):
        e.build_user_data({"a": "pw_12345678"}, **{**ok, "ref": "main"})


def test_launch_runs_one_instance_and_prints_https_paths_and_writes_credentials():
    fake = FakeEc2()
    env, out = launch(fake, hours=2)
    assert env.exit_code == 0 and len(fake.runs) == 1 and fake.runs[0]["type"] == "m6i.large" and fake.runs[0]["sg"] == "sg-123"
    rows = list(csv.DictReader(creds_path("c1").open()))
    assert [r["url"] for r in rows] == ["https://203-0-113-7.sslip.io/learner-01/", "https://203-0-113-7.sslip.io/learner-02/"]
    assert all(r["password"] in fake.runs[0]["ud"] and r["password"] in out for r in rows)      # 2 learners: shown
    assert creds_path("c1").stat().st_mode & 0o777 == 0o600
    assert not any(r["password"] in str(load_state("rag-ide", "c1")) for r in rows)


def test_waits_until_all_ides_answer_then_continues():
    fake = FakeEc2(ready_after=5)
    env, _ = launch(fake)
    assert env.exit_code == 0 and fake.pauses and fake.polls > 5


def test_not_ready_in_time_fails_with_where_to_look(monkeypatch):
    from labagent.packs.rag_ide import pack as pk
    monkeypatch.setattr(pk, "WAIT_S", 45)
    env, _ = launch(FakeEc2(ready_after=10_000))
    assert env.exit_code != 0 and "boot.log" in str(env.__dict__)


def test_account_guard_blocks_before_anything_is_created():
    fake = FakeEc2(authed=False)
    env, _ = launch(fake)
    assert env.exit_code != 0 and "allowed_account_ids" in str(env.__dict__) and fake.runs == []


def test_rerun_keeps_the_running_instance_and_passwords():
    fake = FakeEc2()
    launch(fake)
    first = list(csv.DictReader(creds_path("c1").open()))
    launch(fake)
    assert len(fake.runs) == 1 and list(csv.DictReader(creds_path("c1").open())) == first


def test_destroy_terminates_orphans_deletes_group_and_credentials():
    fake = FakeEc2()
    launch(fake)
    fake.instances["i-orphan"] = True
    env = run_pack(RagIde(), "destroy", "c1", {"platform": "ec2"}, services={"ec2": fake})
    assert env.exit_code == 0 and sorted(fake.terminated) == ["i-0001", "i-orphan"] and fake.sg_deleted and not creds_path("c1").exists()


def test_bad_platform_and_ref_are_rejected():
    fake = FakeEc2()
    assert launch(fake, ref="main")[0].exit_code != 0 and launch(fake, tls="none")[0].exit_code != 0 and fake.runs == []
    assert run_pack(RagIde(), "launch", "c1", {"platform": "gcp"}, services={"ec2": fake}).exit_code != 0


# ---- the real gateway against moto ------------------------------------------------------------------------------------------
boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture
def aws(monkeypatch):
    from labagent.config import Config
    with moto.mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        sess = boto3.Session(region_name="us-east-1")
        ami = sess.client("ec2").describe_images()["Images"][0]["ImageId"]
        cfg = Config(region="us-east-1", allowed_account_ids=["123456789012"])
        gw = e.Ec2Gateway(session=sess, config=cfg)
        gw.ctx.sleep = lambda s: None
        gw.ami = lambda: ami            # moto cannot hold the reserved /aws/ SSM name; the name itself is checked against real AWS by hand
        yield gw, sess


def test_gateway_enforces_the_account_allow_list(aws):
    gw, sess = aws
    assert "123456789012" in gw.check_auth()
    gw.cfg.allowed_account_ids = ["999999999999"]
    with pytest.raises(Exception, match="allowed_account_ids"):
        gw.check_auth()
    gw.cfg.allowed_account_ids = []
    with pytest.raises(Exception, match="allowed_account_ids"):
        gw.check_auth()


def test_gateway_creates_a_locked_down_instance_and_cleans_up(aws):
    gw, sess = aws
    sg = gw.security_group("c1")
    assert gw.security_group("c1") == sg                                             # idempotent
    perms = sess.client("ec2").describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]["IpPermissions"]
    assert sorted(p["FromPort"] for p in perms) == [80, 443]                          # no SSH
    iid = gw.run("c1", sg, "#!/bin/bash\ntrue\n", "m6i.large", spot=False)
    inst = sess.client("ec2").describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    assert inst["MetadataOptions"]["HttpTokens"] == "required" and inst["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    assert "IamInstanceProfile" not in inst                                           # learners get no AWS credentials
    assert {"Key": "cohort", "Value": "c1"} in inst["Tags"] and {"Key": "labagent", "Value": "rag-ide"} in inst["Tags"]
    assert gw.public_ip(iid) and gw.alive(iid) and gw.find("c1") == [iid] and gw.find("other") == []
    gw.terminate([iid])
    assert not gw.alive(iid) and gw.find("c1") == []
    gw.delete_security_group("c1")
    assert not sess.client("ec2").describe_security_groups(Filters=[{"Name": "group-name", "Values": ["labagent-rag-ide-c1"]}])["SecurityGroups"]


def test_gateway_spot_option_is_passed(aws):
    gw, sess = aws
    iid = gw.run("c1", gw.security_group("c1"), "#!/bin/bash\n", "m6i.large", spot=True)
    inst = sess.client("ec2").describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    assert inst.get("InstanceLifecycle") == "spot"


def test_ami_lookup_falls_back_to_describe_images_when_ssm_is_denied(aws):
    gw, sess = aws
    del gw.ami                                           # use the real method, not the test stub
    class Denied:
        def get_parameter(self, **k): raise RuntimeError("AccessDenied")
    real = gw.ctx.client
    gw.ctx.client = lambda svc, region=None: Denied() if svc == "ssm" else real(svc, region)
    calls = {}
    class Ec2Stub:
        def describe_images(self, **k):
            calls.update(k)
            return {"Images": [{"ImageId": "ami-old", "CreationDate": "2026-01-01T00:00:00Z"}, {"ImageId": "ami-new", "CreationDate": "2026-09-01T00:00:00Z"}]}
    gw.ctx.client = lambda svc, region=None: Denied() if svc == "ssm" else Ec2Stub()
    assert gw.ami() == "ami-new" and calls["Owners"] == [e.CANONICAL_OWNER]


def test_dockerfile_hands_the_workspace_to_the_user_before_switching_to_it():
    """Regression: the classic builder makes WORKDIR root-owned even after USER, which broke the first real EC2 boot."""
    from pathlib import Path
    lines = (Path(__file__).parent.parent / "Dockerfile.lab").read_text().splitlines()
    chown = next(i for i, l in enumerate(lines) if "chown coder:coder /home/coder/lab" in l)
    user = next(i for i, l in enumerate(lines) if l.strip() == "USER coder")
    workdir = next(i for i, l in enumerate(lines) if l.startswith("WORKDIR /home/coder/lab"))
    assert chown < user < workdir


def test_self_signed_tls_skips_certificate_verification_only_when_internal():
    f1 = FakeEc2()
    launch(f1, tls="internal")
    assert set(f1.verify_seen) == {False}
    f2 = FakeEc2()
    launch(f2, tls="letsencrypt")
    assert set(f2.verify_seen) == {True}


def test_passwords_survive_a_failure_after_the_instance_starts(monkeypatch):
    """Regression: the credentials file used to be written only at the very end, so a failed wait lost every password."""
    from labagent.packs.rag_ide import pack as pk
    monkeypatch.setattr(pk, "WAIT_S", 30)
    fake = FakeEc2(ready_after=10_000)
    env, _ = launch(fake)
    assert env.exit_code != 0
    rows = list(csv.DictReader(creds_path("c1").open()))
    assert len(rows) == 2 and all(r["password"] in fake.runs[0]["ud"] for r in rows)
    fake.ready_after = 0
    assert launch(fake)[0].exit_code == 0 and len(fake.runs) == 1                      # resume reuses the instance
    assert list(csv.DictReader(creds_path("c1").open())) == rows                     # and keeps the same passwords
