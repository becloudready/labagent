"""azure-labs: copy VMs, disks and network from one resource group to another, snapshot only, and delete what was made."""
import json

import pytest

from labagent.cli import main
from labagent.packs.azure_labs import lifecycle, migrate, safety
from labagent.packs.azure_labs.azcli import AzCli, redact
from labagent.packs.azure_labs.pack import AzureLabs
from labagent.packs.base import PackError, run_pack, state_path

SUB = "00000000-0000-0000-0000-000000000001"
SRC, DST = "azure-labs-legacy", "azure-labs-new"
BASE = f"/subscriptions/{SUB}/resourceGroups/{SRC}/providers"
LOC = "canadacentral"


def rid(group, rtype, name):
    return f"/subscriptions/{SUB}/resourceGroups/{group}/providers/{rtype}/{name}"


def vm_doc(name, size, os_disk, data=(), nic=None, zone=None, **extra):
    return {"name": name, "location": LOC, "zones": [zone] if zone else None, "hardwareProfile": {"vmSize": size},
            "storageProfile": {"osDisk": {"name": os_disk, "managedDisk": {"id": rid(SRC, "Microsoft.Compute/disks", os_disk)}},
                               "dataDisks": [{"name": d, "lun": i, "managedDisk": {"id": rid(SRC, "Microsoft.Compute/disks", d)}} for i, d in enumerate(data)]},
            "networkProfile": {"networkInterfaces": [{"id": rid(SRC, "Microsoft.Network/networkInterfaces", nic or f"{name}-nic")}]}, **extra}


def disk_doc(name, os_type=None, managed_by=None, sku="StandardSSD_LRS", **extra):
    return {"name": name, "id": rid(SRC, "Microsoft.Compute/disks", name), "location": LOC, "sku": {"name": sku}, "osType": os_type,
            "hyperVGeneration": "V2" if os_type else None, "managedBy": managed_by, **extra}


def nic_doc(name, pip=None, nsg=None, ip_configs=1):
    cfg = {"properties": {"subnet": {"id": rid(SRC, "Microsoft.Network/virtualNetworks", "legacy-vnet") + "/subnets/app"},
                          **({"publicIPAddress": {"id": rid(SRC, "Microsoft.Network/publicIPAddresses", pip)}} if pip else {})}}
    return {"name": name, "location": LOC, "properties": {"ipConfigurations": [cfg] * ip_configs, "enableAcceleratedNetworking": False,
                                                          **({"networkSecurityGroup": {"id": rid(SRC, "Microsoft.Network/networkSecurityGroups", nsg)}} if nsg else {})}}


RULE = {"name": "allow-ssh", "properties": {"priority": 100, "direction": "Inbound", "access": "Allow", "protocol": "Tcp", "sourceAddressPrefix": "*",
                                              "sourcePortRange": "*", "destinationAddressPrefix": "*", "destinationPortRange": "22"}}


class FakeAz:
    """In-memory az with a prefilled source group. Records every write so tests can prove what was and was not touched."""

    def __init__(self, usage=None):
        self.subscription = None
        self.writes = []                     # (what, group, name)
        self.groups = {SRC: {"location": LOC, "tags": {}, "resources": []}}
        self.fail_on = {}                    # (what, name) -> remaining failures
        self._usage = usage or {"cores": {"used": 0, "limit": 10}, "standardBSFamily": {"used": 0, "limit": 10}, "standardDSv3Family": {"used": 0, "limit": 10}}
        self.vms = [vm_doc("vm1", "Standard_B1s", "vm1_osdisk", data=("vm1-data",), nic="vm1-nic"),
                    vm_doc("vm2", "Standard_D2s_v3", "vm2_osdisk", nic="vm2-nic", zone="1")]
        self.disks = [disk_doc("vm1_osdisk", "Linux", managed_by="vm1"), disk_doc("vm1-data", managed_by="vm1"),
                      disk_doc("vm2_osdisk", "Windows", managed_by="vm2"), disk_doc("old-dead-disk")]
        self.docs = {
            rid(SRC, "Microsoft.Network/networkInterfaces", "vm1-nic"): nic_doc("vm1-nic", pip="vm1-pip"),
            rid(SRC, "Microsoft.Network/networkInterfaces", "vm2-nic"): nic_doc("vm2-nic"),
            rid(SRC, "Microsoft.Network/virtualNetworks", "legacy-vnet"): {
                "name": "legacy-vnet", "location": LOC, "properties": {"addressSpace": {"addressPrefixes": ["10.30.0.0/16"]}, "subnets": [
                    {"name": "app", "properties": {"addressPrefix": "10.30.1.0/24", "networkSecurityGroup": {"id": rid(SRC, "Microsoft.Network/networkSecurityGroups", "legacy-nsg")}}},
                    {"name": "db", "properties": {"addressPrefix": "10.30.2.0/24"}}]}},
            rid(SRC, "Microsoft.Network/networkSecurityGroups", "legacy-nsg"): {"name": "legacy-nsg", "properties": {"securityRules": [RULE]}},
        }
        self.states = {}

    # reads
    def account(self): return {"id": SUB, "name": "t"}
    def usage(self, region): return self._usage
    def skus(self, region):
        return {"Standard_B1s": {"family": "standardBSFamily", "vcpus": "1", "restricted": 0},
                "Standard_D2s_v3": {"family": "standardDSv3Family", "vcpus": "2", "restricted": 0}}
    def group_exists(self, n): return n in self.groups
    def group_show(self, n):
        g = self.groups.get(n)
        return {"name": n, "location": g["location"], "tags": dict(g["tags"])} if g else None
    def resources(self, g): return [dict(r) for r in self.groups[g]["resources"]]
    def show(self, r): return self.docs[r]
    def list_vms(self, g):
        assert g == SRC
        return self.vms
    def list_disks(self, g):
        assert g == SRC
        return self.disks
    def vm_state(self, g, n): return {"provisioningState": "Succeeded", "powerState": "VM running", "privateIps": "10.30.1.4", "publicIps": ""}

    # writes
    def _add(self, what, rtype, group, name, tags):
        if self.fail_on.get((what, name), 0):
            self.fail_on[(what, name)] -= 1
            raise PackError(f"az {what} failed: boom")
        self.writes.append((what, group, name))
        self.groups[group]["resources"].append({"id": rid(group, rtype, name), "name": name, "type": rtype, "tags": dict(tags)})

    def group_create(self, n, region, tags):
        self.writes.append(("group", n, n))
        self.groups[n] = {"location": region, "tags": dict(tags), "resources": []}
    def group_delete(self, n):
        self.writes.append(("group_delete", n, n))
        del self.groups[n]
    def resource_delete(self, r):
        self.writes.append(("delete", r.split("/resourceGroups/")[1].split("/")[0], r.split("/")[-1]))
        for g in self.groups.values():
            g["resources"] = [x for x in g["resources"] if x["id"] != r]
    def snapshot_create(self, g, n, src, loc, tags, hyper_v=None): self._add("snapshot", "Microsoft.Compute/snapshots", g, n, tags)
    def disk_create(self, g, n, snap, sku, loc, tags, zone=None, hyper_v=None, os_type=None):
        self.disk_args = getattr(self, "disk_args", {}) | {n: (snap, sku, zone, os_type)}
        self._add("disk", "Microsoft.Compute/disks", g, n, tags)
    def nsg_create(self, g, n, loc, tags): self._add("nsg", "Microsoft.Network/networkSecurityGroups", g, n, tags)
    def nsg_rule_create(self, g, nsg, rule): self.writes.append(("rule", g, f"{nsg}/{rule['name']}"))
    def vnet_create(self, g, n, prefixes, sub, pre, loc, tags): self._add("vnet", "Microsoft.Network/virtualNetworks", g, n, tags); self.writes.append(("subnet", g, f"{n}/{sub}"))
    def subnet_create(self, g, vnet, n, prefix): self.writes.append(("subnet", g, f"{vnet}/{n}"))
    def subnet_set_nsg(self, g, vnet, sub, nsg): self.writes.append(("subnet_nsg", g, f"{vnet}/{sub}={nsg}"))
    def public_ip_create(self, g, n, loc, tags): self._add("pip", "Microsoft.Network/publicIPAddresses", g, n, tags)
    def nic_create(self, g, n, vnet, sub, loc, tags, nsg=None, public_ip=None, accelerated=False):
        self.nic_args = getattr(self, "nic_args", {}) | {n: (vnet, sub, nsg, public_ip)}
        self._add("nic", "Microsoft.Network/networkInterfaces", g, n, tags)
    def vm_create(self, g, n, os_disk, os_type, size, nics, data_disks, loc, tags, zone=None):
        self.vm_args = getattr(self, "vm_args", {}) | {n: (os_disk, os_type, size, nics, data_disks, zone)}
        self._add("vm", "Microsoft.Compute/virtualMachines", g, n, tags)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.setenv(safety.ALLOWED_ENV, SUB)


def launch(az, **params):
    return run_pack(AzureLabs(), "launch", "mig", {"source_rg": SRC, "target_rg": DST, **params}, services={"az": az}, log=lambda m: None)


def destroy(az, **params):
    return run_pack(AzureLabs(), "destroy", "mig", params, services={"az": az}, log=lambda m: None)


def names(az, group, rtype):
    return sorted(r["name"] for r in az.groups[group]["resources"] if r["type"].lower() == rtype.lower())


# ---- copying ------------------------------------------------------------------------------------------------------------
def test_launch_copies_vms_disks_and_network_with_the_same_names_and_tags():
    az = FakeAz()
    env = launch(az)
    assert env.status == "ok", env.error
    assert names(az, DST, "Microsoft.Compute/virtualMachines") == ["vm1", "vm2"]
    assert names(az, DST, "Microsoft.Compute/disks") == ["vm1-data", "vm1_osdisk", "vm2_osdisk"]
    assert names(az, DST, "Microsoft.Compute/snapshots") == ["snap-vm1-data", "snap-vm1_osdisk", "snap-vm2_osdisk"]
    assert names(az, DST, "Microsoft.Network/networkInterfaces") == ["vm1-nic", "vm2-nic"]
    assert names(az, DST, "Microsoft.Network/virtualNetworks") == ["legacy-vnet"]
    assert names(az, DST, "Microsoft.Network/networkSecurityGroups") == ["legacy-nsg"]
    assert names(az, DST, "Microsoft.Network/publicIPAddresses") == ["vm1-pip"]
    assert ("rule", DST, "legacy-nsg/allow-ssh") in az.writes
    assert {w[2] for w in az.writes if w[0] == "subnet"} == {"legacy-vnet/app", "legacy-vnet/db"}
    assert ("subnet_nsg", DST, "legacy-vnet/app=legacy-nsg") in az.writes
    assert all(safety.owned(r["tags"], "mig") and r["tags"]["migrated_from"] == SRC for r in az.groups[DST]["resources"])
    assert az.groups[DST]["tags"]["created_by_labagent"] == "1"
    assert "old-dead-disk" not in names(az, DST, "Microsoft.Compute/disks")           # not attached to a VM: only snapshot_only takes it


def test_the_vms_boot_from_the_copied_disks_and_keep_size_zone_and_data_disks():
    az = FakeAz()
    launch(az)
    os_disk, os_type, size, nics, data, zone = az.vm_args["vm1"]
    assert os_disk == rid(DST, "Microsoft.Compute/disks", "vm1_osdisk") and os_type == "linux" and size == "Standard_B1s"
    assert data == [rid(DST, "Microsoft.Compute/disks", "vm1-data")] and nics == [rid(DST, "Microsoft.Network/networkInterfaces", "vm1-nic")] and zone is None
    assert az.vm_args["vm2"][1] == "windows" and az.vm_args["vm2"][5] == "1"
    snap, sku, zone, os_type = az.disk_args["vm2_osdisk"]
    assert snap == rid(DST, "Microsoft.Compute/snapshots", "snap-vm2_osdisk") and sku == "StandardSSD_LRS" and zone == "1" and os_type == "Windows"
    assert az.disk_args["vm1-data"][3] is None                                          # only the OS disk carries an OS type
    assert az.nic_args["vm1-nic"] == ("legacy-vnet", "app", None, "vm1-pip")


def test_the_source_group_is_never_written():
    az = FakeAz()
    launch(az)
    destroy(az)
    assert all(w[1] != SRC for w in az.writes)
    assert len(az.groups[SRC]["resources"]) == 0 and len(az.disks) == 4                 # nothing removed from the source either


def test_a_second_run_creates_nothing_and_a_failed_run_resumes_where_it_stopped():
    az = FakeAz()
    az.fail_on[("vm", "vm2")] = 1
    first = launch(az)
    assert first.status == "failed" and "boom" in first.error and names(az, DST, "Microsoft.Compute/virtualMachines") == ["vm1"]
    second = launch(az)
    assert second.status == "ok" and names(az, DST, "Microsoft.Compute/virtualMachines") == ["vm1", "vm2"]
    before = list(az.writes)
    assert launch(az).status == "ok" and az.writes == before


def test_vms_filter_copies_only_the_named_vms_and_unknown_names_are_an_error():
    az = FakeAz()
    assert launch(az, vms="vm1").status == "ok"
    assert names(az, DST, "Microsoft.Compute/virtualMachines") == ["vm1"] and names(az, DST, "Microsoft.Network/networkInterfaces") == ["vm1-nic"]
    bad = launch(FakeAz(), vms="vm1,nope")
    assert bad.status == "failed" and "nope" in bad.error


def test_snapshot_only_takes_every_disk_including_unattached_ones_and_nothing_else():
    az = FakeAz()
    env = launch(az, snapshot_only=True)
    assert env.status == "ok", env.error
    assert names(az, DST, "Microsoft.Compute/snapshots") == ["snap-old-dead-disk", "snap-vm1-data", "snap-vm1_osdisk", "snap-vm2_osdisk"]
    assert {r["type"] for r in az.groups[DST]["resources"]} == {"Microsoft.Compute/snapshots"}
    only = FakeAz()
    launch(only, snapshot_only=True, vms="vm1")
    assert names(only, DST, "Microsoft.Compute/snapshots") == ["snap-vm1-data", "snap-vm1_osdisk"]


def test_what_cannot_be_copied_is_reported_together_before_anything_is_created():
    az = FakeAz()
    az.vms[0]["availabilitySet"] = {"id": "x"}
    az.vms[1]["storageProfile"]["osDisk"]["managedDisk"] = None
    az.docs[rid(SRC, "Microsoft.Network/networkInterfaces", "vm1-nic")] = nic_doc("vm1-nic", pip="vm1-pip", ip_configs=2)
    env = launch(az)
    assert env.status == "failed" and "availability set" in env.error and "unmanaged" in env.error and "more than one IP" in env.error
    assert DST not in az.groups and not az.writes
    asg = FakeAz()
    asg.docs[rid(SRC, "Microsoft.Network/networkSecurityGroups", "legacy-nsg")]["properties"]["securityRules"] = [
        {**RULE, "properties": {**RULE["properties"], "sourceApplicationSecurityGroups": [{"id": "x"}]}}]
    assert "application security groups" in launch(asg).error
    enc = FakeAz()
    enc.disks[0]["encryption"] = {"diskEncryptionSetId": "x"}
    assert "customer-managed" in launch(enc).error


# ---- refusals -----------------------------------------------------------------------------------------------------------
def test_bad_input_creates_nothing():
    for params, msg in (({"source_rg": None}, "source_rg"), ({"target_rg": SRC}, "must be different"), ({"source_rg": "missing-rg"}, "does not exist")):
        az = FakeAz()
        env = run_pack(AzureLabs(), "launch", "mig", {"source_rg": SRC, "target_rg": DST, **params}, services={"az": az}, log=lambda m: None)
        assert env.status == "failed" and msg in env.error and not az.writes


def test_the_subscription_allow_list_is_enforced_for_launch_and_destroy(monkeypatch):
    for value in ("", "11111111-1111-1111-1111-111111111111"):
        monkeypatch.setenv(safety.ALLOWED_ENV, value)
        az = FakeAz()
        assert launch(az).status == "failed" and "refusing to run" in launch(az).error and not az.writes
        assert destroy(az, target_rg=DST).status == "failed"


def test_not_enough_quota_stops_before_any_copy_and_says_why():
    az = FakeAz(usage={"cores": {"used": 8, "limit": 10}, "standardBSFamily": {"used": 1, "limit": 10}, "standardDSv3Family": {"used": 0, "limit": 10}})
    env = launch(az)
    assert env.status == "failed" and "total regional vCPUs: need 3, free 2" in env.error and "source VMs still count" in env.error and not az.writes
    assert launch(FakeAz(usage={"cores": {"used": 0, "limit": 10}}), snapshot_only=True).status == "ok"   # snapshots need no vCPUs


def test_dry_run_reads_nothing():
    az = FakeAz()
    env = run_pack(AzureLabs(), "launch", "mig", {}, services={"az": az}, dry_run=True)
    assert env.status == "ok" and not az.writes and len(env.items) == 7


# ---- destroy -----------------------------------------------------------------------------------------------------------
def test_destroy_deletes_what_the_migration_made_dependants_first_and_the_group():
    az = FakeAz()
    launch(az)
    env = destroy(az)
    assert env.status == "ok" and DST not in az.groups and not state_path("azure-labs", "mig").exists()
    deleted = [w[2] for w in az.writes if w[0] == "delete"]
    assert deleted.index("vm1") < deleted.index("vm1-nic") < deleted.index("vm1_osdisk") < deleted.index("snap-vm1_osdisk") < deleted.index("legacy-vnet")


def test_destroy_never_deletes_an_untagged_resource_and_keeps_a_group_it_did_not_create():
    az = FakeAz()
    launch(az)
    az.groups[DST]["resources"].append({"id": rid(DST, "Microsoft.Compute/disks", "precious"), "name": "precious", "type": "Microsoft.Compute/disks", "tags": {}})
    env = destroy(az)
    assert env.status == "failed" and "precious" in env.error and DST in az.groups
    assert ("delete", DST, "precious") not in az.writes
    pre = FakeAz()
    pre.groups[DST] = {"location": LOC, "tags": {"team": "x"}, "resources": [{"id": "i", "name": "keep-me", "type": "Microsoft.Compute/disks", "tags": {}}]}
    launch(pre)
    assert destroy(pre).status == "ok" and DST in pre.groups and [r["name"] for r in pre.groups[DST]["resources"]] == ["keep-me"]


def test_destroy_never_works_on_the_source_group_and_needs_a_target():
    az = FakeAz()
    launch(az)
    assert "refusing to delete from the source" in destroy(az, target_rg=SRC).error
    assert "target_rg" in run_pack(AzureLabs(), "destroy", "other", {}, services={"az": FakeAz()}, log=lambda m: None).error


def test_destroy_reports_a_resource_it_cannot_delete():
    az = FakeAz()
    launch(az)
    real = az.resource_delete
    az.resource_delete = lambda r: (_ for _ in ()).throw(PackError("in use")) if r.endswith("/legacy-vnet") else real(r)
    env = destroy(az)
    assert env.status == "failed" and "legacy-vnet" in env.error and DST in az.groups


# ---- the az wrapper -----------------------------------------------------------------------------------------------------
class Proc:
    def __init__(self, out="", err="", rc=0):
        self.stdout, self.stderr, self.returncode = out, err, rc


def recorder(out="{}"):
    seen = []
    return seen, AzCli("sub-1", run=lambda cmd, **kw: seen.append((cmd, kw)) or Proc(out), binary="az")


def test_azcli_builds_the_commands_the_copy_depends_on():
    seen, az = recorder()
    az.vm_create("g", "vm1", "DISK", "linux", "Standard_B1s", ["NIC"], ["D1", "D2"], "canadacentral", {"a": "1"}, zone="1")
    cmd = seen[-1][0]
    assert cmd[:6] == ["az", "vm", "create", "-g", "g", "-n"] and "--attach-os-disk" in cmd and cmd[cmd.index("--attach-data-disks") + 1:cmd.index("--attach-data-disks") + 3] == ["D1", "D2"]
    assert cmd[cmd.index("--zone") + 1] == "1" and "--tags" in cmd and cmd[-2:] == ["--subscription", "sub-1"]
    az.snapshot_create("g", "s", "SRC", "canadacentral", {"a": "1"}, "V2")
    cmd = seen[-1][0]
    assert cmd[cmd.index("--incremental") + 1] == "false" and cmd[cmd.index("--hyper-v-generation") + 1] == "V2" and cmd[cmd.index("--source") + 1] == "SRC"
    az.nsg_rule_create("g", "nsg", migrate._rule(RULE))
    cmd = seen[-1][0]
    assert cmd[cmd.index("--destination-port-ranges") + 1] == "22" and cmd[cmd.index("--priority") + 1] == "100"
    assert all(kw["encoding"] == "utf-8" and kw["text"] is True for _, kw in seen)


def test_azcli_errors_are_redacted_and_a_missing_binary_is_explained(monkeypatch):
    az = AzCli(run=lambda cmd, **kw: Proc(err="ERROR: bad adminPassword=Sup3rSecret! token: abcdef123456", rc=1), binary="az")
    with pytest.raises(PackError) as e:
        az.json("vm", "create")
    assert "Sup3rSecret" not in str(e.value) and "abcdef123456" not in str(e.value) and "Sup3rSecret" not in redact("password: Sup3rSecret!")
    monkeypatch.setattr("shutil.which", lambda n: None)
    with pytest.raises(PackError, match="Azure CLI not found"):
        AzCli().json("account", "show")


def test_azcli_group_exists_usage_and_the_size_list_is_fetched_once():
    assert AzCli(run=lambda c, **k: Proc("true"), binary="az").group_exists("g") is True
    usage = [{"name": {"value": "cores"}, "currentValue": "2", "limit": "10"}]
    assert AzCli(run=lambda c, **k: Proc(json.dumps(usage)), binary="az").usage("r") == {"cores": {"used": 2, "limit": 10}}
    seen, az = recorder(json.dumps([{"name": "Standard_B1s", "family": "standardBSFamily", "restricted": 0, "vcpus": "1"}]))
    az.skus("r"), az.skus("r")
    assert len(seen) == 1


def test_the_pack_is_listed_with_no_credential(capsys):
    assert main(["packs", "--json"]) == 0
    out = capsys.readouterr().out
    assert {p["id"]: p for p in json.loads(out)["packs"]}["azure-labs"]["platforms"] == ["azure"] and SUB not in out
