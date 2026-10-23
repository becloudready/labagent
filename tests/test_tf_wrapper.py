"""The wrapper drives a fake `terraform` executable, so these run anywhere without Terraform or AWS."""
import json
import os
import re
import stat
import sys

import pytest

from labagent.config import Config
from labagent.tf import Terraform, TerraformError, redact

FAKE = r'''#!{py}
import json, os, stat, sys
log = os.environ["FAKE_TF_LOG"]
args = sys.argv[1:]
sub = [a for a in args if not a.startswith("-chdir")][0]
rec = {{"sub": sub, "args": args, "data_dir": os.environ.get("TF_DATA_DIR"), "in_automation": os.environ.get("TF_IN_AUTOMATION")}}
vf = [a.split("=", 1)[1] for a in args if a.startswith("-var-file=")]
if vf:
    rec["var_mode"] = oct(stat.S_IMODE(os.stat(vf[0]).st_mode))
    rec["vars"] = json.load(open(vf[0]))
    rec["var_path"] = vf[0]
open(log, "a").write(json.dumps(rec) + "\n")
if os.environ.get("FAKE_TF_FAIL") == sub:
    sys.stderr.write("Error: user ann-lee@example.com already exists\nsecond line\n")
    sys.exit(1)
if sub == "show":
    print(json.dumps({{"resource_changes": [
        {{"type": "aws_iam_user", "index": "7", "change": {{"actions": ["create"]}}}},
        {{"type": "aws_iam_group", "index": None, "change": {{"actions": ["create"]}}}}]}}))
if sub == "output":
    print(json.dumps({{"console_url": {{"value": "https://x"}}, "users": {{"value": {{"7": "ann-lee@example.com"}}}}}}))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    exe = tmp_path / "terraform"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_TF_LOG", str(log))
    cfg = Config(terraform_bin=str(exe), backend={"bucket": "b", "region": "us-east-1", "kms_key_id": "arn:kms"})
    return Terraform(cfg), log


def calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()]


VARS = {"cohort": "c1", "learners": {"7": {"username": "ann-lee@example.com"}}}


def test_apply_sequence_backend_config_and_secure_varfile(fake):
    tf, log = fake
    out = tf.apply("c1", VARS)
    c = calls(log)
    assert [x["sub"] for x in c] == ["init", "apply", "output"]
    init = " ".join(c[0]["args"])
    assert "-lockfile=readonly" in init and "-reconfigure" in init
    assert "-backend-config=bucket=b" in init and "-backend-config=key=cohorts/c1/terraform.tfstate" in init
    assert "-backend-config=encrypt=true" in init and "-backend-config=use_lockfile=true" in init
    assert "-backend-config=kms_key_id=arn:kms" in init
    assert c[1]["var_mode"] == "0o600" and c[1]["vars"]["learners"]["7"]["username"] == "ann-lee@example.com"
    assert "-auto-approve" in c[1]["args"] and "-lock-timeout=5m" in c[1]["args"]
    assert c[1]["in_automation"] == "1"
    assert not os.path.exists(c[1]["var_path"])             # roster var-file removed afterwards
    assert out["users"] == {"7": "ann-lee@example.com"}


def test_state_data_dir_is_outside_the_module(fake):
    tf, log = fake
    tf.apply("c1", VARS)
    assert str(tf.module_dir) not in calls(log)[0]["data_dir"]


def test_plan_is_read_only_and_parses_changes(fake):
    tf, log = fake
    plan = tf.plan("c1", VARS)
    c = calls(log)
    assert [x["sub"] for x in c] == ["init", "plan", "show"]
    assert "-lock=false" in c[1]["args"]
    assert plan["create"] == 2 and plan["users_to_create"] == ["7"]


def test_destroy(fake):
    tf, log = fake
    tf.destroy("c1", VARS)
    assert [x["sub"] for x in calls(log)] == ["init", "destroy"]


def test_error_is_redacted_and_varfile_still_removed(fake, monkeypatch):
    tf, log = fake
    monkeypatch.setenv("FAKE_TF_FAIL", "apply")
    with pytest.raises(TerraformError) as e:
        tf.apply("c1", VARS)
    assert "ann-lee@example.com" not in str(e.value) and "<redacted>" in str(e.value)
    assert not os.path.exists(calls(log)[-1]["var_path"])


def test_missing_backend_is_refused_before_any_command(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_TF_LOG", str(tmp_path / "l"))
    tf = Terraform(Config(terraform_bin="/bin/true"))
    with pytest.raises(TerraformError, match="backend.bucket"):
        tf.apply("c1", VARS)
    assert not (tmp_path / "l").exists()


def test_backend_env_overrides(monkeypatch):
    monkeypatch.setenv("LABAGENT_TF_BACKEND_BUCKET", "from-env")
    assert Config(backend={"bucket": "from-file"}).backend_settings()["bucket"] == "from-env"


def test_redact():
    assert "@" not in redact("failed for a@example.com and c.d@example.org")


def test_unknown_config_keys_are_rejected_not_ignored(tmp_path):
    import pytest
    from labagent.config import load_config
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"group_name": "old", "name_prefix": "x"}))
    with pytest.raises(ValueError, match="group_name"):
        load_config(str(p))


def test_module_never_manages_passwords():
    """Secrets must never enter Terraform state: the module must not declare a login profile."""
    from labagent.config import MODULE_DIR
    text = "".join(p.read_text() for p in MODULE_DIR.glob("*.tf"))
    assert not re.search(r'resource\s+"aws_iam_user_login_profile"', text)


def test_only_labagent_env_vars_are_read(monkeypatch):
    from labagent.config import getenv
    monkeypatch.delenv("LABAGENT_TF_BIN", raising=False)
    monkeypatch.setenv("LABCTL_TF_BIN", "/old/terraform")
    assert getenv("TF_BIN") is None                                  # the pre-rename prefix is no longer honoured
    monkeypatch.setenv("LABAGENT_TF_BIN", "/new/terraform")
    assert getenv("TF_BIN") == "/new/terraform"


def test_packaged_terraform_modules_are_present():
    from labagent.config import MODULE_DIR, STATE_BACKEND_DIR
    assert (MODULE_DIR / "main.tf").exists() and (STATE_BACKEND_DIR / "main.tf").exists()
    assert not list(STATE_BACKEND_DIR.glob("*.tfstate*")), "state files must never live inside the package"
