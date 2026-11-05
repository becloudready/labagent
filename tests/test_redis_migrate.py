"""redis-migrate: the rules on hand-written cache documents, and the pack end to end with a fake `az`.

The documents follow the field names in Azure's own SDK models (azure-mgmt-redis). They are not recordings: Microsoft no longer
lets new customers create a legacy cache, so a real one could not be captured.
"""
import json
import subprocess

import pytest

from labagent.packs.azure_labs.azcli import AzCli
from labagent.packs.base import run_pack
from labagent.packs.redis_migrate import rules
from labagent.packs.redis_migrate.pack import RedisMigrate

SUB = "00000000-0000-0000-0000-000000000001"
CACHE_ID = f"/subscriptions/{SUB}/resourceGroups/rg1/providers/Microsoft.Cache/Redis/legacy"
REGIONS = {"canadacentral", "eastus"}


def cache(**over):
    doc = {"id": CACHE_ID, "name": "legacy", "resourceGroup": "rg1", "location": "Canada Central", "provisioningState": "Succeeded",
           "sku": {"name": "Premium", "family": "P", "capacity": 1}, "redisVersion": "6.0.14", "enableNonSslPort": False,
           "redisConfiguration": {}, "shardCount": None, "subnetId": None, "linkedServers": []}
    doc.update(over)
    return doc


def storage(**over):
    doc = {"name": "stg", "id": f"/subscriptions/{SUB}/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/stg",
           "kind": "StorageV2", "location": "canadacentral", "isHnsEnabled": False, "networkRuleSet": {"defaultAction": "Allow"}}
    doc.update(over)
    return doc


def facts(c=None, **over):
    base = {"cache": c or cache(), "links": [], "private_endpoints": 0, "storage": None, "target_region": None, "amr_regions": REGIONS}
    base.update(over)
    return base


def codes(findings, level):
    return {f["code"] for f in findings if f["level"] == level}


def test_plain_premium_cache_has_no_blockers():
    out = rules.assess(facts())
    assert codes(out, "blocker") == set()
    assert {"other-databases", "cross-slot"} <= codes(out, "warning")


@pytest.mark.parametrize("tier", ["Basic", "Standard"])
def test_basic_and_standard_cannot_export(tier):
    out = rules.assess(facts(cache(sku={"name": tier, "family": "C", "capacity": 1})))
    assert "no-export" in codes(out, "blocker")


@pytest.mark.parametrize("over,code", [
    ({"subnetId": "/subscriptions/x/virtualNetworks/v/subnets/s"}, "vnet-injection"),
    ({"linkedServers": [{"id": "/x"}]}, "geo-replication"),
    ({"provisioningState": "Scaling"}, "not-running"),
    ({"redisVersion": "8.2.0"}, "rdb-version"),
])
def test_blockers(over, code):
    assert code in codes(rules.assess(facts(cache(**over))), "blocker")


def test_geo_replication_is_found_through_server_links_too():
    assert "geo-replication" in codes(rules.assess(facts(links=[{"name": "peer"}])), "blocker")


@pytest.mark.parametrize("over,code", [
    ({"shardCount": 3}, "clustered"),
    ({"enableNonSslPort": True}, "non-tls-port"),
    ({"redisConfiguration": {"notify-keyspace-events": "KEA"}}, "keyspace-notifications"),
    ({"redisConfiguration": {"rdb-backup-enabled": "true"}}, "persistence"),
    ({"redisConfiguration": {"aof-backup-enabled": "true"}}, "persistence"),
])
def test_warnings(over, code):
    assert code in codes(rules.assess(facts(cache(**over))), "warning")


def test_defaults_do_not_trigger_the_optional_warnings():
    w = codes(rules.assess(facts(cache(redisConfiguration={"rdb-backup-enabled": "false", "notify-keyspace-events": ""}))), "warning")
    assert not ({"persistence", "keyspace-notifications", "non-tls-port"} & w)


def test_private_endpoints_warn_and_unknown_is_only_info():
    assert "private-endpoints" in codes(rules.assess(facts(private_endpoints=2)), "warning")
    assert "private-endpoints" in codes(rules.assess(facts(private_endpoints=None)), "info")


def test_target_region_without_amr_warns_and_cross_region_is_noted():
    out = rules.assess(facts(target_region="Mars North"))
    assert "amr-region" in codes(out, "warning") and "cross-region" in codes(out, "info")
    assert "amr-region" in codes(rules.assess(facts(amr_regions=None)), "info")


def test_region_names_compare_without_spaces_or_case():
    assert rules.norm_region("Canada Central") == rules.norm_region("canadacentral")
    assert "amr-region" not in codes(rules.assess(facts(target_region="Canada Central")), "warning")


def test_no_storage_account_is_skipped_not_failed():
    assert "storage" in codes(rules.assess(facts()), "info")


@pytest.mark.parametrize("over,code,level", [
    ({"kind": "BlobStorage"}, "storage-kind", "blocker"),
    ({"isHnsEnabled": True}, "storage-adls", "blocker"),
    ({"location": "eastus"}, "storage-region", "blocker"),
    ({"networkRuleSet": {"defaultAction": "Deny"}}, "storage-firewall", "warning"),
    ({"id": "/subscriptions/99999999-0000-0000-0000-000000000009/resourceGroups/r/providers/Microsoft.Storage/storageAccounts/stg"},
     "storage-subscription", "warning"),
])
def test_storage_rules(over, code, level):
    assert code in codes(rules.assess(facts(storage=storage(**over))), level)


def test_good_storage_account_adds_nothing():
    out = rules.assess(facts(storage=storage()))
    assert not [f for f in out if f["code"].startswith("storage-")]


# ---- the pack, with a fake az ----------------------------------------------------------------------------------------------
def fake_az(c, stg=None, links=(), pes=(), regions=("Canada Central", "East US")):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        a = " ".join(cmd[1:])
        if a.startswith("account show"):
            out = {"id": SUB}
        elif a.startswith("redis show"):
            out = c
        elif a.startswith("redis list"):
            out = [c]
        elif a.startswith("redis server-link list"):
            out = list(links)
        elif a.startswith("network private-endpoint-connection list"):
            out = list(pes)
        elif a.startswith("storage account list"):
            out = [stg] if stg else []
        elif a.startswith("provider show"):
            out = list(regions)
        else:
            return subprocess.CompletedProcess(cmd, 1, "", f"unexpected: {a}")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
    return AzCli(run=run, binary="az"), calls


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))


def launch(az, **params):
    given = {"source_cache": "legacy", **params}
    return run_pack(RedisMigrate(), "launch", "check", given, services={"az": az}, log=lambda m: None)


def test_pack_reports_warnings_and_exits_clean():
    az, calls = fake_az(cache())
    env = launch(az, source_rg="rg1")
    assert env.status == "ok" and env.exit_code == 0
    out = env.summary["outputs"]
    assert out["verdict"] == "warnings" and out["cache"]["tier"] == "Premium"
    assert all(c[1:3] != ["redis", "export"] for c in calls)                       # read only
    assert not any(w in " ".join(c) for c in calls for w in ("create", "delete", "import", "export"))


def test_pack_blocks_a_standard_cache_and_names_the_reason():
    az, _ = fake_az(cache(sku={"name": "Standard", "family": "C", "capacity": 2}))
    env = launch(az, source_rg="rg1")
    assert env.status == "failed" and env.exit_code == 2
    assert "no-export" in env.error


def test_pack_finds_the_cache_by_name_when_no_resource_group_is_given():
    az, calls = fake_az(cache())
    assert launch(az).status == "ok"
    assert any(c[1:3] == ["redis", "list"] for c in calls)


def test_pack_checks_the_storage_account_when_given():
    az, _ = fake_az(cache(), stg=storage(kind="BlobStorage"))
    env = launch(az, source_rg="rg1", storage_account="stg")
    assert env.status == "failed" and "storage-kind" in env.error


def test_pack_fails_clearly_when_the_storage_account_is_missing():
    az, _ = fake_az(cache(), stg=None)
    env = launch(az, source_rg="rg1", storage_account="nope")
    assert env.status == "failed" and "nope" in env.error


def test_a_missing_optional_fact_does_not_fail_the_check():
    az, _ = fake_az(cache(), regions=())
    env = launch(az, source_rg="rg1")
    assert env.status == "ok"
    assert "amr-region" in codes(env.summary["outputs"]["findings"], "info")


def test_source_cache_is_required():
    az, _ = fake_az(cache())
    env = run_pack(RedisMigrate(), "launch", "check", {}, services={"az": az}, log=lambda m: None)
    assert env.status == "failed" and "source_cache" in env.error


def test_destroy_has_nothing_to_remove():
    az, _ = fake_az(cache())
    env = run_pack(RedisMigrate(), "destroy", "check", {"source_cache": "legacy"}, services={"az": az}, log=lambda m: None)
    assert env.status == "ok"


def test_dry_run_needs_no_azure():
    env = run_pack(RedisMigrate(), "launch", "check", {"source_cache": "legacy"}, dry_run=True)
    assert [i["step"] for i in env.items] == ["validate"]
