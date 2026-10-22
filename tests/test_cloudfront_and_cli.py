import json
import re
import subprocess
import sys

from labagent.context import Ctx
from labagent.deleters import del_distribution
from labagent.discovery import Resource


class FakeCF:
    def __init__(self, enabled, status):
        self.enabled, self.status, self.deleted = enabled, status, False

    def get_distribution_config(self, Id):
        return {"ETag": "e", "DistributionConfig": {"Enabled": self.enabled}}

    def update_distribution(self, Id, IfMatch, DistributionConfig):
        self.enabled = DistributionConfig["Enabled"]

    def get_distribution(self, Id):
        return {"Distribution": {"Status": self.status}}

    def delete_distribution(self, Id, IfMatch):
        self.deleted = True


def run(cf, cfg):
    ctx = Ctx(cfg)
    ctx._clients[("cloudfront", "us-east-1")] = cf
    r = Resource("arn:x", "cloudfront", "distribution", "E1", "us-east-1", "E1")
    return del_distribution(ctx, r)


def test_cloudfront_disable_then_wait_then_delete(cfg):
    cf = FakeCF(enabled=True, status="Deployed")
    assert run(cf, cfg).state == "pending" and cf.enabled is False
    cf.status = "InProgress"
    assert run(cf, cfg).state == "pending" and not cf.deleted
    cf.status = "Deployed"
    assert run(cf, cfg).state == "deleted" and cf.deleted


def test_cli_failure_has_exit_code_2_and_valid_envelope(tmp_path):
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"command": "inventory", "cohort": {"id": "c1"}, "learners": []}))
    out = tmp_path / "result.json"
    p = subprocess.run([sys.executable, "-m", "labagent.cli", "run", "--job", str(job), "--output", str(out)],
                       capture_output=True, text=True, env={"PATH": "/usr/bin", "AWS_ACCESS_KEY_ID": "x",
                                                            "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1"})
    env = json.loads(out.read_text())
    assert p.returncode == 2 and env["status"] == "failed"      # empty allow-list: refuses to run
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_logs_contain_no_emails(ctx):
    """stderr is retained by CI. It must carry ids and counts only."""
    import io, contextlib
    from labagent import sweep, iam_ops
    from labagent.discovery import Scope
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        sweep.inventory(ctx, Scope(cohort_id="c1"), [{"learner_id": "7", "username": "ann-lee@example.com"}])
        sweep.sweep_plan(ctx, Scope(cohort_id="c1"), [{"learner_id": "7", "username": "ann-lee@example.com"}])
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", buf.getvalue())


def test_cloudfront_dangling_s3_origin_is_neutralized_so_it_can_be_disabled(cfg):
    import boto3
    from botocore.exceptions import ClientError
    from moto import mock_aws

    class DanglingCF(FakeCF):
        def __init__(self):
            super().__init__(enabled=True, status="Deployed")
            self.cfg = {"Enabled": True, "Origins": {"Items": [
                {"Id": "a", "DomainName": "gone-bucket.s3.us-east-1.amazonaws.com", "S3OriginConfig": {"OriginAccessIdentity": ""}, "OriginAccessControlId": "OAC1"},
                {"Id": "b", "DomainName": "kept-bucket.s3.us-east-1.amazonaws.com", "S3OriginConfig": {"OriginAccessIdentity": ""}},
                {"Id": "c", "DomainName": "api.example.org", "CustomOriginConfig": {}}]}}
            self.updates = 0

        def get_distribution_config(self, Id):
            return {"ETag": "e", "DistributionConfig": self.cfg}

        def update_distribution(self, Id, IfMatch, DistributionConfig):
            self.updates += 1
            if any(o["DomainName"].startswith("gone-bucket") for o in DistributionConfig["Origins"]["Items"]):
                raise ClientError({"Error": {"Code": "InvalidArgument", "Message": "The parameter S3 bucket has been deleted."}}, "UpdateDistribution")
            self.enabled = DistributionConfig["Enabled"]
            self.cfg = DistributionConfig

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="kept-bucket")
        ctx = Ctx(cfg, boto3.Session(region_name="us-east-1"))
        cf = DanglingCF()
        ctx._clients[("cloudfront", "us-east-1")] = cf
        r = Resource("arn:x", "cloudfront", "distribution", "E1", "us-east-1", "E1")
        out = del_distribution(ctx, r)
    assert out.state == "pending" and cf.updates == 2 and cf.enabled is False
    origins = {o["Id"]: o for o in cf.cfg["Origins"]["Items"]}
    assert origins["a"]["DomainName"] == "example.com" and "S3OriginConfig" not in origins["a"]
    custom = origins["a"]["CustomOriginConfig"]                              # CloudFront requires all of these
    assert custom["OriginSslProtocols"]["Items"] == ["TLSv1.2"] and custom["OriginProtocolPolicy"] == "https-only"
    assert custom["OriginReadTimeout"] == 30 and custom["OriginKeepaliveTimeout"] == 5
    assert custom["HTTPPort"] == 80 and custom["HTTPSPort"] == 443
    assert origins["a"]["OriginAccessControlId"] == ""
    assert origins["b"]["DomainName"].startswith("kept-bucket")            # existing bucket untouched
    assert origins["c"]["DomainName"] == "api.example.org"                  # non-S3 untouched


def test_cli_bootstrap_path_and_legacy_alias(capsys):
    from labagent import cli
    assert cli.main(["bootstrap-path"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("state_backend")
