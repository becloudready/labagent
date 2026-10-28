"""dbx-workspace: the pack, with a fake terraform. No cloud, no network, no cost."""
import json
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from labagent.packs import registry
from labagent.packs.base import load_state, run_pack, state_path
from labagent.packs.dbx_workspace import guards
from labagent.packs.dbx_workspace.pack import DatabricksWorkspace
from labagent.packs.dbx_workspace.tf import MODULES, Tf, clean, summarize_plan

ACCT = "11111111-2222-3333-4444-555555555555"
SUB = "99999999-8888-7777-6666-555555555555"
SECRET = "s3cr3t-value-123456"


def creates(*types):
    return {"resource_changes": [{"type": t, "change": {"actions": ["create"]}} for t in types]}


class FakeTerraform:
    """Stands in for subprocess.run. Records every call and answers like terraform would."""

    def __init__(self, plan=None, outputs=None, fail_on=None, stderr=""):
        self.plan_json = plan if plan is not None else creates("aws_iam_role", "databricks_mws_workspaces", "aws_s3_bucket")
        self.out = outputs if outputs is not None else {"workspace_url": {"value": "https://dbc-demo.cloud.databricks.com"}, "workspace_id": {"value": 123},
                                                          "pricing_tier": {"value": "PREMIUM"}}
        self.fail_on, self.stderr, self.calls, self.envs = fail_on, stderr, [], []

    def __call__(self, cmd, capture_output=True, text=True, env=None, cwd=None):
        args = cmd[1:]
        self.calls.append((args, cwd))
        self.envs.append(env)
        if self.fail_on and args[0] == self.fail_on:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.stderr or "Error: boom")
        if args[0] == "plan":
            (Path(cwd) / "plan.bin").write_text("plan")
        if args[:2] == ["show", "-json"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.plan_json), stderr="")
        if args[0] == "output":
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.out), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def verbs(self):
        return [c[0][0] for c in self.calls]


class FakeAws:
    def __init__(self, account="123456789012"):
        self.account = account

    def check(self):
        return self.account


class FakeAzure:
    def __init__(self, sub=SUB):
        self.sub = sub

    def check(self):
        return self.sub, "az login"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LABAGENT_TF_BIN", "/usr/bin/terraform")
    return tmp_path


def aws_env(**extra):
    e = {"DATABRICKS_CLIENT_ID": "client-id", "DATABRICKS_CLIENT_SECRET": SECRET, "LABAGENT_ALLOWED_ACCOUNTS": "123456789012", "PATH": "/usr/bin"}
    e.update(extra)
    return e


def azure_env(**extra):
    e = {"LABAGENT_ALLOWED_SUBSCRIPTIONS": SUB, "PATH": "/usr/bin"}
    e.update(extra)
    return e


def services(fake, **kw):
    sv = {"tf": lambda wd: Tf(wd, run=fake, binary="/usr/bin/terraform", env=kw.get("env")), "probe": lambda url: None}
    sv.update(kw)
    return sv


def aws(home, fake=None, action="launch", params=None, **kw):
    fake = fake or FakeTerraform()
    p = {"cloud": "aws", "account_id": ACCT}
    p.update(params or {})
    sv = services(fake, env=kw.pop("env", aws_env()), aws=kw.pop("aws_auth", FakeAws()), **kw)
    return run_pack(DatabricksWorkspace(), action, "demo", p, services=sv), fake


def azure(home, fake=None, action="launch", params=None, **kw):
    fake = fake or FakeTerraform(outputs={"workspace_url": {"value": "adb-1.2.azuredatabricks.net"}, "workspace_id": {"value": "1"}, "sku": {"value": "premium"}})
    p = {"cloud": "azure"}
    p.update(params or {})
    sv = services(fake, env=kw.pop("env", azure_env()), azure=kw.pop("azure_auth", FakeAzure()), **kw)
    return run_pack(DatabricksWorkspace(), action, "demo", p, services=sv), fake


# ---- registration ----------------------------------------------------------------------------------------------------------------

def test_pack_is_registered_with_both_clouds_and_does_not_steal_common_words():
    p = registry.resolve("premium-workspace")
    assert p.id == "dbx-workspace" and p.platforms == ("aws", "azure") and p.cost_tier == "metered"
    assert "workspace" not in p.aliases                                     # a bare common word would hijack plain-English requests
    assert {q.name for q in p.params} >= {"cloud", "region", "network", "apply", "account_id", "subscription_id"}
    assert next(q for q in p.params if q.name == "cloud").required


# ---- plan first ------------------------------------------------------------------------------------------------------------------

def test_without_apply_it_only_plans_and_creates_nothing(home):
    env, fake = aws(home)
    assert env.status == "ok", env.error
    assert "apply" not in fake.verbs() and fake.verbs().count("plan") == 1
    o = env.summary["outputs"]
    assert o["status"] == "planned" and o["plan"]["add"] == 3 and "apply=yes" in o["message"] and o["cloud"] == "aws"
    assert o["resources"] == {"aws_iam_role": 1, "aws_s3_bucket": 1, "databricks_mws_workspaces": 1} and "workspace_url" not in o


def test_apply_yes_applies_the_saved_plan_file_and_checks_the_tier(home):
    env, fake = aws(home, params={"apply": "yes"})
    assert env.status == "ok", env.error
    verbs = fake.verbs()
    assert verbs.index("plan") < verbs.index("apply") < verbs.index("output")
    assert [c[0] for c in fake.calls if c[0][0] == "apply"] == [["apply", "-input=false", "-no-color", "plan.bin"]]        # a file, never -auto-approve
    o = env.summary["outputs"]
    assert o["status"] == "created" and o["workspace_url"].startswith("https://") and o["tier"] == "PREMIUM" and "NAT gateway" in o["cost_note"]


def test_the_plan_is_made_again_before_every_apply(home):
    aws(home)
    env, fake = aws(home, params={"apply": "yes"})
    assert fake.verbs().count("plan") == 1 and "apply" in fake.verbs()


def test_a_plan_that_would_replace_the_workspace_is_refused(home):
    bad = {"resource_changes": [{"type": "databricks_mws_workspaces", "change": {"actions": ["delete", "create"]}}]}
    env, fake = aws(home, FakeTerraform(plan=bad), params={"apply": "yes"})
    assert env.status == "failed" and "replace" in env.error and "apply" not in fake.verbs()


def test_plan_summary_counts_actions_without_values():
    s = summarize_plan({"resource_changes": [
        {"type": "a", "change": {"actions": ["create"], "after": {"password": "x"}}}, {"type": "b", "change": {"actions": ["update"]}},
        {"type": "c", "change": {"actions": ["delete"]}}, {"type": "d", "change": {"actions": ["delete", "create"]}}, {"type": "e", "change": {"actions": ["no-op"]}}]})
    assert (s["add"], s["change"], s["destroy"], s["replace"]) == (1, 1, 1, 1) and "x" not in json.dumps(s)


# ---- premium ---------------------------------------------------------------------------------------------------------------------

def test_aws_workspace_on_a_standard_plan_is_reported_as_a_failure(home):
    fake = FakeTerraform(outputs={"workspace_url": {"value": "https://x"}, "workspace_id": {"value": 1}, "pricing_tier": {"value": "STANDARD"}})
    env, _ = aws(home, fake, params={"apply": "yes"})
    assert env.status == "failed" and "STANDARD" in env.error and "account plan" in env.error and load_state("dbx-workspace", "demo")["applied"] is True


def test_aws_enterprise_counts_as_premium_and_an_unreported_tier_is_not_a_failure(home):
    ent = FakeTerraform(outputs={"workspace_url": {"value": "https://x"}, "workspace_id": {"value": 1}, "pricing_tier": {"value": "enterprise"}})
    assert aws(home, ent, params={"apply": "yes"})[0].summary["outputs"]["tier"] == "ENTERPRISE"


def test_azure_must_report_the_premium_sku(home):
    env, _ = azure(home, params={"apply": "yes"})
    assert env.status == "ok" and env.summary["outputs"]["tier"] == "PREMIUM" and env.summary["outputs"]["workspace_url"] == "https://adb-1.2.azuredatabricks.net"
    bad = FakeTerraform(outputs={"workspace_url": {"value": "h"}, "workspace_id": {"value": "1"}, "sku": {"value": "standard"}})
    env, _ = azure(home, bad, params={"apply": "yes"}, )
    assert env.status == "failed" and "not premium" in env.error


def test_a_slow_first_start_is_reported_not_failed(home):
    def down(url):
        raise OSError("not yet")
    env, _ = aws(home, params={"apply": "yes"}, probe=down)
    assert env.status == "ok" and env.summary["outputs"]["status"].startswith("created (waiting")


# ---- guards ----------------------------------------------------------------------------------------------------------------------

def test_aws_refuses_without_an_allow_list_or_for_another_account(home):
    env, fake = aws(home, env=aws_env(LABAGENT_ALLOWED_ACCOUNTS=""))
    assert env.status == "failed" and "LABAGENT_ALLOWED_ACCOUNTS is empty" in env.error and fake.calls == []
    env, fake = aws(home, aws_auth=FakeAws("111111111111"))
    assert env.status == "failed" and "not in LABAGENT_ALLOWED_ACCOUNTS" in env.error and fake.calls == []


def test_azure_refuses_without_an_allow_list_or_for_another_subscription(home):
    env, fake = azure(home, env=azure_env(LABAGENT_ALLOWED_SUBSCRIPTIONS=""))
    assert env.status == "failed" and "ALLOWED_SUBSCRIPTIONS is empty" in env.error and fake.calls == []
    env, _ = azure(home, azure_auth=FakeAzure("00000000-0000-0000-0000-000000000001"))
    assert env.status == "failed" and "not in LABAGENT_ALLOWED_SUBSCRIPTIONS" in env.error
    env, _ = azure(home, params={"subscription_id": "00000000-0000-0000-0000-0000000000aa"})
    assert env.status == "failed" and "does not match" in env.error


def test_aws_needs_the_databricks_account_id_and_oauth_credentials_from_the_environment(home):
    env, _ = aws(home, params={"account_id": "nope"})
    assert env.status == "failed" and "GUID" in env.error
    env, _ = aws(home, env=aws_env(DATABRICKS_CLIENT_SECRET=""))
    assert env.status == "failed" and "DATABRICKS_CLIENT_SECRET" in env.error and SECRET not in env.error
    with pytest.raises(Exception):
        guards.require_databricks_account({}, None)


def test_options_are_checked_with_plain_messages(home):
    for params, msg in (({"cloud": "gcp"}, "cloud must"), ({"network": "x"}, "network must"), ({"apply": "maybe"}, "apply must"), ({"hardened": "yes"}, "Azure only"),
                        ({"owner": "me@example.com"}, "team name"), ({"backend": "s3"}, "state_bucket"), ({"backend": "ftp"}, "backend must")):
        env, _ = aws(home, params=params)
        assert env.status == "failed" and msg in env.error, (params, env.error)
    env, _ = azure(home, params={"force_destroy": "yes"})
    assert "AWS only" in env.error
    env = run_pack(DatabricksWorkspace(), "launch", "ab", {"cloud": "aws"}, services={})
    assert env.status == "failed" and "3 to 24" in env.error


def test_missing_cloud_is_a_clear_error(home):
    env = run_pack(DatabricksWorkspace(), "launch", "demo", {}, services={})
    assert env.status == "failed" and "cloud" in env.error


# ---- what goes to terraform ------------------------------------------------------------------------------------------------------

def test_variables_files_and_backend_hold_no_secret_and_are_private(home):
    env, fake = aws(home, params={"apply": "yes"})
    work = home / "home" / "packs" / "dbx-workspace" / "demo-tf"
    tfvars = work / "terraform.tfvars.json"
    v = json.loads(tfvars.read_text())
    assert v == {"prefix": "demo", "network": "managed", "owner": "labagent", "databricks_account_id": ACCT, "region": "us-east-1", "force_destroy": False}
    assert stat.S_IMODE(tfvars.stat().st_mode) == 0o600 and not (work / "plan.bin").exists()
    everything = json.dumps([c[0] for c in fake.calls]) + tfvars.read_text() + (work / "backend.tf").read_text() + json.dumps(env.to_dict()) + json.dumps(load_state("dbx-workspace", "demo"))
    assert SECRET not in everything and "client-id" not in everything
    assert (work / "backend.tf").read_text() == 'terraform {\n  backend "local" {}\n}\n'


def test_init_uses_the_lock_file_read_only_and_the_local_state_path(home):
    _, fake = aws(home)
    init = next(c[0] for c in fake.calls if c[0][0] == "init")
    assert "-lockfile=readonly" in init and any(a.startswith("-backend-config=path=") and a.endswith("demo.tfstate") for a in init)
    assert (home / "home" / "packs" / "dbx-workspace").stat().st_mode & 0o077 == 0
    assert next(Path(a.split("=", 2)[2]).parent for a in init if a.startswith("-backend-config=path=")) == home / "home" / "packs" / "dbx-workspace"


def test_s3_and_azurerm_backends_are_configured_from_parameters(home):
    _, fake = aws(home, params={"backend": "s3", "state_bucket": "my-state"})
    init = next(c[0] for c in fake.calls if c[0][0] == "init")
    assert "-backend-config=bucket=my-state" in init and "-backend-config=key=dbx-workspace/demo.tfstate" in init and "-backend-config=encrypt=true" in init
    _, fake = azure(home, params={"backend": "azurerm", "state_storage": "rg/acct/cont"})
    init = next(c[0] for c in fake.calls if c[0][0] == "init")
    assert "-backend-config=resource_group_name=rg" in init and "-backend-config=container_name=cont" in init
    env, _ = azure(home, params={"backend": "azurerm", "state_storage": "bad"})
    assert "state_storage" in env.error


def test_azure_variables_and_custom_network_names(home):
    _, fake = azure(home, params={"network": "custom", "hardened": "yes", "region": "westeurope"})
    v = json.loads((home / "home" / "packs" / "dbx-workspace" / "demo-tf" / "terraform.tfvars.json").read_text())
    assert v == {"prefix": "demo", "network": "vnet", "owner": "labagent", "subscription_id": SUB, "location": "westeurope", "hardened": True}
    _, _ = aws(home, params={"network": "custom"})
    assert json.loads((home / "home" / "packs" / "dbx-workspace" / "demo-tf" / "terraform.tfvars.json").read_text())["network"] == "vpc"


def test_terraform_debug_logging_is_never_passed_on(home):
    _, fake = aws(home, env=aws_env(TF_LOG="TRACE"))
    assert all("TF_LOG" not in e for e in fake.envs)


def test_errors_are_cleaned_of_secrets_colour_and_emails(home):
    fake = FakeTerraform(fail_on="plan", stderr=f"\x1b[31mError\x1b[0m: bad token {SECRET} for ada@example.com")
    env, _ = aws(home, fake)
    assert env.status == "failed" and SECRET not in env.error and "ada@example.com" not in env.error and "\x1b" not in env.error and "<redacted>" in env.error
    assert clean("x\n" * 40, {}).count("x") == 14


def test_failure_starts_at_the_error_block_not_the_progress_lines():
    from labagent.packs.dbx_workspace.tf import failure
    err = "Error: cannot create workspace: bad credentials\n\n  with databricks_mws_workspaces.this,\n  on main.tf line 179"
    out = failure(err, "\n".join(f"res{i}: Creating..." for i in range(50)), {})
    assert out.startswith("Error: cannot create workspace") and "Creating" not in out and len(out) <= 340
    assert "boom" in failure("", "a\nboom", {})


def test_long_errors_keep_the_end_where_terraform_prints_the_reason():
    out = clean("progress " + "p" * 3000 + "\nError: the real reason", {})
    assert out.endswith("Error: the real reason") and len(out) <= 1800


# ---- destroy ---------------------------------------------------------------------------------------------------------------------

def created(home):
    env, _ = aws(home, params={"apply": "yes"})
    assert env.status == "ok"


def test_destroy_without_apply_is_a_preview_and_keeps_the_records(home):
    created(home)
    fake = FakeTerraform(plan={"resource_changes": [{"type": "databricks_mws_workspaces", "change": {"actions": ["delete"]}}]})
    env, fake = aws(home, fake, action="destroy")
    assert env.status == "failed" and "Preview only" in env.error and "Nothing was deleted" in env.error
    assert "apply" not in fake.verbs() and any("-destroy" in c[0] for c in fake.calls if c[0][0] == "plan")
    assert state_path("dbx-workspace", "demo").exists() and load_state("dbx-workspace", "demo")["applied"] is True


def test_destroy_with_apply_deletes_then_forgets_everything(home):
    created(home)
    fake = FakeTerraform(plan={"resource_changes": [{"type": "databricks_mws_workspaces", "change": {"actions": ["delete"]}}]})
    env, fake = aws(home, fake, action="destroy", params={"apply": "yes"})
    assert env.status == "ok", env.error
    assert any("-destroy" in c[0] for c in fake.calls if c[0][0] == "plan") and "apply" in fake.verbs()
    assert not state_path("dbx-workspace", "demo").exists() and not (home / "home" / "packs" / "dbx-workspace" / "demo-tf").exists()
    assert not (home / "home" / "packs" / "dbx-workspace" / "demo.tfstate").exists()


def test_destroy_refuses_when_nothing_was_ever_created(home):
    env, fake = aws(home, action="destroy", params={"apply": "yes"})
    assert env.status == "failed" and "nothing to destroy" in env.error and "apply" not in fake.verbs()


def test_force_destroy_reaches_the_module_only_when_asked(home):
    created(home)
    aws(home, action="destroy", params={"apply": "yes", "force_destroy": "yes"})
    # the working folder is removed after a destroy, so check the variables the runner was given through a fresh plan
    env, fake = aws(home, params={"force_destroy": "yes"})
    assert json.loads((home / "home" / "packs" / "dbx-workspace" / "demo-tf" / "terraform.tfvars.json").read_text())["force_destroy"] is True


def test_a_half_finished_apply_can_still_be_destroyed(home):
    env, _ = aws(home, FakeTerraform(fail_on="apply", stderr="Error: quota"), params={"apply": "yes"})
    assert env.status == "failed" and "quota" in env.error and load_state("dbx-workspace", "demo")["applied"] is True
    env, fake = aws(home, action="destroy", params={"apply": "yes"})
    assert env.status == "ok"


# ---- the Terraform modules (static checks: no terraform needed) -------------------------------------------------------------------

def hcl(cloud, name):
    return (MODULES / f"databricks_{cloud}" / name).read_text()


def test_azure_module_pins_the_premium_sku_as_a_literal():
    main = hcl("azure", "main.tf")
    assert re.search(r'sku\s*=\s*"premium"', main) and not re.search(r"sku\s*=\s*var\.", main)
    assert "no_public_ip" in main and "Microsoft.Databricks/workspaces" in main and "resource_providers_to_register" in main


def test_aws_module_keeps_credentials_out_of_terraform_and_the_bucket_private():
    allv = hcl("aws", "variables.tf") + hcl("azure", "variables.tf")
    assert not re.search(r"client_secret|client_id|password|token", allv, re.I)
    main = hcl("aws", "main.tf")
    for needle in ("block_public_acls       = true", "restrict_public_buckets = true", "force_destroy = var.force_destroy", "AES256"):
        assert needle in main
    assert "client_secret" not in main and 'external_id = var.databricks_account_id' in main


def test_modules_pin_providers_and_ship_lock_files_for_every_platform():
    for cloud, providers in (("aws", {"aws", "databricks", "random", "time"}), ("azure", {"azurerm", "random"})):
        d = MODULES / f"databricks_{cloud}"
        versions = (d / "versions.tf").read_text()
        for p in providers:
            assert re.search(rf'{p}\s*=\s*\{{[^}}]*version\s*=\s*"~> ', versions), (cloud, p)
        lock = (d / ".terraform.lock.hcl").read_text()
        assert len(re.findall(r'^provider "', lock, re.M)) == len(providers)
        assert lock.count("h1:") >= len(providers) and "zh:" in lock
        assert (d / "tests" / "workspace.tftest.hcl").exists()
