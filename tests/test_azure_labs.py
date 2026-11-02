"""azure-labs, first slice: safety guards, az wrapper, network step, tag-only destroy, expiry sweep, key handling."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from labagent import azure_sweep, secrets_box
from labagent.cli import main
from labagent.packs.azure_labs import lifecycle, safety
from labagent.packs.azure_labs.azcli import AzCli, redact
from labagent.packs.azure_labs.pack import AzureLabs
from labagent.packs.base import PackError, run_pack, state_path

SUB = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
GROUP = "rg-labagent-azure-labs-demo"


class FakeAz:
    """In-memory stand-in for AzCli. Records every call so tests can prove what was and was not touched."""

    def __init__(self, sub=SUB, usage=None, restricted=False):
        self.subscription = None
        self.sub = sub
        self.groups = {}                     # name -> {"location", "tags", "resources": [..]}
        self.calls = []
        self.deploys = []
        self.restricted = restricted
        self.undeletable = set()
        self._usage = usage or {"cores": {"used": 0, "limit": 10}, "standardBSFamily": {"used": 0, "limit": 10},
                                "standardDv3Family": {"used": 0, "limit": 10}}

    def account(self):
        self.calls.append("account")
        return {"id": self.sub, "name": "test"}

    def usage(self, region):
        return self._usage

    def sku_family(self, size, region):
        if self.restricted:
            raise PackError(f"{size} is restricted in {region} for this subscription. Pick another size or region.")
        return "standardBSFamily" if size.startswith("Standard_B") else "standardDv3Family"

    def group_exists(self, name):
        return name in self.groups

    def group_show(self, name):
        g = self.groups.get(name)
        return {"name": name, "id": f"/subscriptions/{self.sub}/resourceGroups/{name}", "location": g["location"], "tags": dict(g["tags"])} if g else None

    def group_create(self, name, region, tags):
        self.calls.append(("group_create", name))
        self.groups[name] = {"location": region, "tags": dict(tags), "resources": []}

    def group_delete(self, name):
        self.calls.append(("group_delete", name))
        del self.groups[name]

    def groups_with_tag(self, key, value):
        return [self.group_show(n) for n, g in self.groups.items() if g["tags"].get(key) == value]

    def resources(self, group):
        return [dict(r) for r in self.groups[group]["resources"]]

    def resource_delete(self, rid):
        self.calls.append(("resource_delete", rid))
        if rid in self.undeletable:
            raise PackError("az resource delete failed: in use")
        for g in self.groups.values():
            g["resources"] = [r for r in g["resources"] if r["id"] != rid]

    def deploy(self, group, template, parameters, name):
        self.calls.append(("deploy", group))
        self.deploys.append((group, template, parameters))
        tags = parameters["tags"]
        for rtype, rname in (("Microsoft.Network/virtualNetworks", "vnet-x"), ("Microsoft.Network/networkSecurityGroups", "nsg-x")):
            self.groups[group]["resources"].append({"id": f"{group}/{rname}", "name": rname, "type": rtype, "tags": dict(tags)})
        return {}

    def add(self, group, name, rtype, tags):
        self.groups[group]["resources"].append({"id": f"{group}/{name}", "name": name, "type": rtype, "tags": tags})


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.setenv(safety.ALLOWED_ENV, SUB)


def launch(az, **params):
    return run_pack(AzureLabs(), "launch", "demo", params, services={"az": az}, log=lambda m: None)


def destroy(az, name="demo"):
    return run_pack(AzureLabs(), "destroy", name, {}, services={"az": az}, log=lambda m: None)


# ---- allow-list and validation ------------------------------------------------------------------------------------------
def test_refuses_when_the_allow_list_is_empty_or_does_not_list_the_subscription(monkeypatch):
    for value in ("", "11111111-1111-1111-1111-111111111111"):
        monkeypatch.setenv(safety.ALLOWED_ENV, value)
        az = FakeAz()
        env = launch(az)
        assert env.status == "failed" and "refusing to run" in env.error
        assert not az.groups and ("group_create", GROUP) not in az.calls


def test_the_allow_list_is_case_insensitive_and_takes_a_comma_list(monkeypatch):
    monkeypatch.setenv(safety.ALLOWED_ENV, f"other, {SUB.upper()}")
    assert launch(FakeAz()).status == "ok"


@pytest.mark.parametrize("params,msg", [
    ({"vm_size": "Standard_M128s"}, "not allowed"),
    ({"nested": True, "vm_size": "Standard_B2s"}, "nested-virtualization"),
    ({"students": 31}, "max_students"),
    ({"students": 0}, "between 1"),
    ({"max_students": 101}, "between 1 and 100"),
    ({"region": "Canada Central"}, "region"),
    ({"os": "windows-10"}, "os must be"),
    ({"expires_in_days": 400}, "between 0 and 365"),
])
def test_bad_parameters_are_refused_before_anything_is_created(params, msg):
    az = FakeAz()
    env = launch(az, **params)
    assert env.status == "failed" and msg in env.error and not az.groups


def test_defaults_follow_the_os_and_nested_uses_a_nested_size():
    base = {"students": 3, "region": "canadacentral", "expires_in_days": 1}
    assert safety.validate({"os": "linux", **base})["vm_size"] == "Standard_B2s"
    assert safety.validate({"os": "windows-server", **base})["vm_size"] == "Standard_D2s_v3"
    assert safety.validate({"os": "linux", "nested": True, **base})["vm_size"] in safety.NESTED


def test_names_are_cleaned_and_must_be_long_enough():
    assert safety.group_name("Demo Lab_1") == "rg-labagent-azure-labs-demo-lab-1"
    with pytest.raises(PackError):
        safety.lab_name("a!")


# ---- quota ------------------------------------------------------------------------------------------------------------
def test_quota_counts_the_busiest_phase_not_the_sum():
    total, fams = lifecycle.peak_demand("standardBSFamily", 2, 3, "standardBSFamily", 2)
    assert total == 8 and fams == {"standardBSFamily": 8}              # 3 students x 2 + gateway 2; the template phase is smaller
    total, fams = lifecycle.peak_demand("standardDv3Family", 2, 2, "standardBSFamily", 2)
    assert total == 6 and fams == {"standardDv3Family": 4, "standardBSFamily": 2}


def test_not_enough_quota_names_the_family_and_creates_nothing():
    az = FakeAz(usage={"cores": {"used": 0, "limit": 10}, "standardBSFamily": {"used": 0, "limit": 10}, "standardDv3Family": {"used": 0, "limit": 0}})
    env = launch(az, os="windows-server", students=2)
    assert env.status == "failed" and "standardDv3Family: need 4, free 0" in env.error and not az.groups


def test_regional_quota_is_checked_too():
    env = launch(FakeAz(), students=5)                                 # 5 x 2 + 2 = 12 > 10
    assert env.status == "failed" and "total regional vCPUs: need 12, free 10" in env.error


def test_a_restricted_size_is_reported():
    env = launch(FakeAz(restricted=True))
    assert env.status == "failed" and "restricted" in env.error


# ---- network step -----------------------------------------------------------------------------------------------------
def test_launch_creates_a_tagged_group_and_network_and_rerun_skips_it():
    az = FakeAz()
    env = launch(az, students=3, expires_in_days=7)
    assert env.status == "ok" and [i["state"] for i in env.items] == ["done", "done"]
    g = az.groups[GROUP]
    assert g["tags"]["labagent"] == "1" and g["tags"]["pack"] == "azure-labs" and g["tags"]["created_by_labagent"] == "1"
    assert g["tags"]["expires_at"] != safety.NEVER and {"created_at", "name", "cohort"} <= set(g["tags"])
    assert all(r["tags"]["labagent"] == "1" for r in g["resources"])
    assert "created_by_labagent" not in g["resources"][0]["tags"]
    again = launch(az, students=3, expires_in_days=7)
    assert [i["state"] for i in again.items] == ["done", "skipped"]    # preflight always runs; the network is already there
    assert az.calls.count(("group_create", GROUP)) == 1
    assert env.summary["outputs"]["group"] == GROUP


def test_expires_in_days_zero_means_never():
    az = FakeAz()
    launch(az, expires_in_days=0)
    assert az.groups[GROUP]["tags"]["expires_at"] == safety.NEVER


def test_a_group_with_the_same_name_that_is_not_ours_is_refused():
    az = FakeAz()
    az.groups[GROUP] = {"location": "canadacentral", "tags": {"team": "finance"}, "resources": []}
    env = launch(az)
    assert env.status == "failed" and "not this lab's" in env.error and not az.deploys


def test_the_network_template_ships_and_has_no_public_inbound_except_443():
    t = json.loads((Path(__file__).parent.parent / "labagent/packs/azure_labs/arm/network.json").read_text(encoding="utf-8"))
    rules = {r["name"]: r["properties"] for res in t["resources"] if "securityRules" in res["properties"] for r in res["properties"]["securityRules"]}
    assert rules["allow-https"]["destinationPortRange"] == "443" and rules["allow-https"]["sourceAddressPrefix"] == "Internet"
    assert rules["allow-gateway-ssh-rdp"]["sourceAddressPrefix"] == "[variables('gatewayPrefix')]"
    assert rules["deny-other-vnet"]["access"] == "Deny"
    assert all(r["tags"] == "[parameters('tags')]" for r in t["resources"])


# ---- destroy -----------------------------------------------------------------------------------------------------------
def test_destroy_deletes_tagged_resources_then_the_group_and_the_state():
    az = FakeAz()
    launch(az)
    assert state_path("azure-labs", "demo").exists()
    env = destroy(az)
    assert env.status == "ok" and GROUP not in az.groups
    assert not state_path("azure-labs", "demo").exists()


def test_destroy_never_deletes_an_untagged_resource_and_keeps_the_group():
    az = FakeAz()
    launch(az)
    az.add(GROUP, "precious-disk", "Microsoft.Compute/disks", {})
    az.add(GROUP, "other-lab-vm", "Microsoft.Compute/virtualMachines", {"labagent": "1", "pack": "azure-labs", "name": "someone-else"})
    env = destroy(az)
    assert env.status == "failed" and "precious-disk" in env.error and "other-lab-vm" in env.error
    deleted = [c[1] for c in az.calls if isinstance(c, tuple) and c[0] == "resource_delete"]
    assert f"{GROUP}/precious-disk" not in deleted and f"{GROUP}/other-lab-vm" not in deleted
    assert GROUP in az.groups and ("group_delete", GROUP) not in az.calls
    assert {r["name"] for r in az.groups[GROUP]["resources"]} == {"precious-disk", "other-lab-vm"}   # the lab's own network is gone


def test_destroy_deletes_dependants_first():
    az = FakeAz()
    launch(az)
    tags = dict(az.groups[GROUP]["resources"][0]["tags"])
    az.add(GROUP, "nic-1", "Microsoft.Network/networkInterfaces", tags)
    az.add(GROUP, "vm-1", "Microsoft.Compute/virtualMachines", tags)
    destroy(az)
    order = [c[1].split("/")[-1] for c in az.calls if isinstance(c, tuple) and c[0] == "resource_delete"]
    assert order.index("vm-1") < order.index("nic-1") < order.index("vnet-x") < order.index("nsg-x")


def test_destroy_reports_a_resource_it_cannot_delete():
    az = FakeAz()
    launch(az)
    az.undeletable.add(f"{GROUP}/vnet-x")
    env = destroy(az)
    assert env.status == "failed" and "vnet-x" in env.error and GROUP in az.groups


def test_destroy_keeps_a_group_labagent_did_not_create_and_refuses_a_foreign_one():
    az = FakeAz()
    tags = safety.lab_tags("demo", "", 7, NOW)                       # tagged for this lab, but without created_by_labagent
    az.groups[GROUP] = {"location": "canadacentral", "tags": tags, "resources": []}
    az.add(GROUP, "vnet-1", "Microsoft.Network/virtualNetworks", tags)
    env = destroy(az)
    assert env.status == "ok" and GROUP in az.groups and az.groups[GROUP]["resources"] == []
    az.groups["rg-labagent-azure-labs-other"] = {"location": "canadacentral", "tags": {"team": "x"}, "resources": []}
    env = destroy(az, "other")
    assert env.status == "failed" and "refusing to touch" in env.error


def test_destroy_with_no_group_is_a_clean_no_op():
    assert destroy(FakeAz()).status == "ok"


def test_destroy_also_checks_the_allow_list(monkeypatch):
    monkeypatch.setenv(safety.ALLOWED_ENV, "")
    az = FakeAz()
    az.groups[GROUP] = {"location": "x", "tags": safety.lab_tags("demo", "", 7), "resources": []}
    assert destroy(az).status == "failed" and GROUP in az.groups


def test_dry_run_makes_no_azure_call():
    az = FakeAz()
    env = run_pack(AzureLabs(), "launch", "demo", {}, services={"az": az}, dry_run=True)
    assert env.status == "ok" and az.calls == []


# ---- sweep ----------------------------------------------------------------------------------------------------------------
def lab_group(az, name, expires, role=None, pack="azure-labs"):
    tags = {"labagent": "1", "pack": pack, "name": name, "cohort": name, "created_at": safety.stamp(NOW - timedelta(days=9)), "expires_at": expires}
    if role:
        tags["role"] = role
    g = f"rg-labagent-azure-labs-{name}"
    az.groups[g] = {"location": "canadacentral", "tags": {**tags, "created_by_labagent": "1"}, "resources": []}
    az.add(g, "vnet-1", "Microsoft.Network/virtualNetworks", tags)
    return g


def approved(az, now=NOW):
    env = azure_sweep.sweep_plan(az, SUB, now)
    return {**env.plan, "items": env.items}


def test_sweep_plan_lists_only_expired_labagent_labs():
    az = FakeAz()
    lab_group(az, "old", safety.stamp(NOW - timedelta(days=1)))
    lab_group(az, "fresh", safety.stamp(NOW + timedelta(days=3)))
    lab_group(az, "forever", safety.NEVER)
    lab_group(az, "oldimages", safety.stamp(NOW - timedelta(days=1)), role="images")
    lab_group(az, "otherpack", safety.stamp(NOW - timedelta(days=1)), pack="not-ours")
    env = azure_sweep.sweep_plan(az, SUB, NOW)
    by = {i["id"]: i["action"] for i in env.items}
    assert by["rg-labagent-azure-labs-old"] == "delete"
    assert [by[f"rg-labagent-azure-labs-{n}"] for n in ("fresh", "forever", "oldimages")] == ["skip", "skip", "skip"]
    assert "rg-labagent-azure-labs-otherpack" not in by
    assert env.summary == {"to_delete": 1, "skipped": 3} and env.plan["hash"]


def test_sweep_apply_needs_the_approved_hash_and_deletes_only_the_plan():
    az = FakeAz()
    g = lab_group(az, "old", safety.stamp(NOW - timedelta(days=1)))
    plan = approved(az)
    bad = azure_sweep.sweep_apply(az, SUB, plan, "0" * 64, NOW)
    assert bad.status == "failed" and "approved hash" in bad.error and g in az.groups
    lab_group(az, "later", safety.stamp(NOW - timedelta(days=1)))   # expired after the plan was made: must not be deleted
    env = azure_sweep.sweep_apply(az, SUB, plan, plan["hash"], NOW)
    assert env.status == "ok" and g not in az.groups and "rg-labagent-azure-labs-later" in az.groups


def test_sweep_apply_skips_a_renewed_lab_and_rejects_an_old_plan_and_a_wrong_subscription(monkeypatch):
    az = FakeAz()
    g = lab_group(az, "old", safety.stamp(NOW - timedelta(days=1)))
    plan = approved(az)
    az.groups[g]["tags"]["expires_at"] = safety.stamp(NOW + timedelta(days=5))
    env = azure_sweep.sweep_apply(az, SUB, plan, plan["hash"], NOW)
    assert env.items[0]["state"] == "skipped" and g in az.groups
    late = azure_sweep.sweep_apply(az, SUB, plan, plan["hash"], NOW + timedelta(hours=25))
    assert late.status == "failed" and "expired" in late.error
    monkeypatch.setenv(safety.ALLOWED_ENV, "11111111-1111-1111-1111-111111111111")
    with pytest.raises(PackError):
        azure_sweep.sweep_plan(az, SUB, NOW)


def test_sweep_apply_never_deletes_an_untagged_resource_in_an_expired_lab():
    az = FakeAz()
    g = lab_group(az, "old", safety.stamp(NOW - timedelta(days=1)))
    az.add(g, "precious", "Microsoft.Compute/disks", {})
    plan = approved(az)
    env = azure_sweep.sweep_apply(az, SUB, plan, plan["hash"], NOW)
    assert env.status == "partial" and env.items[0]["state"] == "failed" and "precious" in env.items[0]["error"]
    assert g in az.groups and [r["name"] for r in az.groups[g]["resources"]] == ["precious"]


# ---- the az wrapper -----------------------------------------------------------------------------------------------------
class Proc:
    def __init__(self, out="", err="", rc=0):
        self.stdout, self.stderr, self.returncode = out, err, rc


def test_azcli_builds_commands_with_json_output_and_the_subscription():
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw))
        return Proc("[]")
    AzCli("sub-1", run=run, binary="/usr/bin/az").resources("rg-x")
    cmd, kw = seen[0]
    assert cmd == ["/usr/bin/az", "resource", "list", "-g", "rg-x", "--only-show-errors", "-o", "json", "--subscription", "sub-1"]
    assert kw["encoding"] == "utf-8" and kw["text"] is True


def test_azcli_errors_are_redacted_and_never_include_the_arguments():
    az = AzCli(run=lambda cmd, **kw: Proc(err="ERROR: bad request adminPassword=Sup3rSecret! and token: abcdef123456", rc=1), binary="az")
    with pytest.raises(PackError) as e:
        az.json("vm", "create", "--admin-password", "Sup3rSecret!")
    msg = str(e.value)
    assert "Sup3rSecret" not in msg and "abcdef123456" not in msg and "<redacted>" in msg
    assert "Sup3rSecret" not in redact("password: Sup3rSecret!")


def test_azcli_group_exists_and_a_missing_az_binary(monkeypatch):
    assert AzCli(run=lambda c, **k: Proc("true"), binary="az").group_exists("g") is True
    assert AzCli(run=lambda c, **k: Proc("false"), binary="az").group_exists("g") is False
    monkeypatch.setattr("shutil.which", lambda n: None)
    with pytest.raises(PackError, match="Azure CLI not found"):
        AzCli().json("account", "show")


def test_azcli_deploy_keeps_secrets_off_the_command_line_and_removes_the_file():
    seen = {}

    def run(cmd, **kw):
        path = cmd[cmd.index("--parameters") + 1].lstrip("@")
        seen.update(cmd=cmd, path=path, mode=stat.S_IMODE(os.stat(path).st_mode), body=json.loads(open(path, encoding="utf-8").read()))
        return Proc("{}")
    AzCli(run=run, binary="az").deploy("rg", "t.json", {"adminPassword": "Sup3rSecret!", "lab": "x"}, "d1")
    assert "Sup3rSecret!" not in " ".join(seen["cmd"]) and seen["mode"] == 0o600
    assert seen["body"]["parameters"]["adminPassword"] == {"value": "Sup3rSecret!"}
    assert not os.path.exists(seen["path"])


def test_azcli_usage_and_sku_family_parse_az_output():
    usage = [{"name": {"value": "cores"}, "currentValue": "2", "limit": "10"}]
    assert AzCli(run=lambda c, **k: Proc(json.dumps(usage)), binary="az").usage("canadacentral") == {"cores": {"used": 2, "limit": 10}}
    calls = []

    def run(c, **k):
        calls.append(c)
        return Proc(json.dumps([{"name": "Standard_B2s", "family": "standardBSFamily", "restricted": 0},
                                {"name": "Standard_D2s_v5", "family": "standardDSv5Family", "restricted": 1}]))
    az = AzCli(run=run, binary="az")
    assert az.sku_family("Standard_B2s", "canadacentral") == "standardBSFamily"
    with pytest.raises(PackError, match="restricted"):
        az.sku_family("Standard_D2s_v5", "canadacentral")
    with pytest.raises(PackError, match="not available"):
        az.sku_family("Standard_Nope", "canadacentral")
    assert len(calls) == 1                                             # one az call for every lookup in the region


# ---- keys and secrets -----------------------------------------------------------------------------------------------------
def test_keypair_round_trip_and_the_cli_decrypts_an_envelope(tmp_path, capsys):
    assert main(["secrets", "keygen", "--dir", str(tmp_path)]) == 0
    priv, pub = tmp_path / "labagent-private.pem", tmp_path / "labagent-public.pem"
    assert stat.S_IMODE(priv.stat().st_mode) == 0o600
    assert main(["secrets", "keygen", "--dir", str(tmp_path)]) == 2          # never overwrite a key
    ct = secrets_box.encrypt(secrets_box.load_public_key(pub.read_text()), "Pa55-word")
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"secrets": [{"user": "student-01", "kind": "vm_password", "ciphertext": ct}]}))
    capsys.readouterr()
    assert main(["secrets", "decrypt", "--key", str(priv), "--in", str(result)]) == 0
    assert capsys.readouterr().out.strip() == "student-01\tvm_password\tPa55-word"
    assert main(["secrets", "decrypt", "--key", str(tmp_path / "nope.pem"), "--in", str(result)]) == 2


def test_passwords_a_pack_creates_are_sealed_with_the_pubkey_and_use_the_pack_kind():
    from labagent.packs.base import Pack, Step
    priv, pub = secrets_box.generate_keypair()

    class Mint(Pack):
        id = "mint"
        params = []

        def deploy_steps(self, ctx):
            def run(c):
                c.mem["passwords"] = {"s1": "Secret-1"}
                c.mem["password_kind"] = "vm_password"
                return {}
            return [Step("mint", "Make a password", run)]

        def destroy_steps(self, ctx):
            return []
    env = run_pack(Mint(), "launch", "x", {}, services={}, pubkey_pem=pub)
    assert env.secrets[0]["kind"] == "vm_password" and "Secret-1" not in json.dumps(env.to_dict())
    assert secrets_box.decrypt(secrets_box.load_private_key(priv), env.secrets[0]["ciphertext"]) == "Secret-1"
    assert run_pack(Mint(), "launch", "y", {}, services={}).secrets == []     # no key: nothing leaves


def test_launch_reads_the_pubkey_from_the_flag_or_the_env(tmp_path, monkeypatch):
    from labagent import cli
    _, pub = secrets_box.generate_keypair()
    f = tmp_path / "k.pem"
    f.write_text(pub)
    assert cli._read_pubkey(str(f)) == pub
    monkeypatch.setenv("LABAGENT_PUBKEY", str(f))
    assert cli._read_pubkey(None) == pub
    monkeypatch.delenv("LABAGENT_PUBKEY")
    assert cli._read_pubkey(None) is None


def test_the_new_commands_are_not_sent_to_the_plain_english_translator():
    from labagent.cli import KNOWN
    assert {"sweep-azure", "secrets"} <= KNOWN


def test_packs_json_lists_azure_labs_with_no_credential(capsys):
    assert main(["packs", "--json"]) == 0
    out = capsys.readouterr().out
    cat = {p["id"]: p for p in json.loads(out)["packs"]}
    assert cat["azure-labs"]["platforms"] == ["azure"] and SUB not in out
