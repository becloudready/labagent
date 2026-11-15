"""Nebius AI Cloud as a compute provider, against a fake Nebius API (no network, no token, no cost).

The fake follows the REST routes and JSON shapes of Nebius's OpenAPI file (https://api.nebius.cloud/openapi.json, read on 2026-11-14): paged lists with
`items` and `nextPageToken`, resources as `metadata` / `spec` / `status`, operations with `resourceId`, errors as `{"code": n, "message": ...}`.
NO real Nebius call has been made by these tests or while writing the provider: nothing here proves a launch works on a real account.
"""
import json
import subprocess
from types import SimpleNamespace

import pytest

from labagent.compute import nebius, providers
from labagent.compute.errors import ComputeError
from labagent.compute.nebius import Auth, NebiusProvider, family_of, nebius_area, sign_jwt
from labagent.compute.providers import ApiError, HostSpec, OfferQuery, catalog
from labagent.packs.base import load_state, state_path
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, host_name, host_tag
from tests.test_gpu_notebooks import FP1, FakeSsh, PUB1, home, run  # noqa: F401  (home is a fixture)
from tests.test_gpu_ssh_keys import ticks

PROJECT = "project-test0000000000000"
SUBNET = "vpcsubnet-test000000000"


def _preset(name, gpus, vcpus, mem, vram=None):
    return {"name": name, "resources": {"gpuCount": gpus, "vcpuCount": vcpus, "memoryGibibytes": mem, "gpuMemoryGibibytes": (vram or 80) * gpus}}


def _platform(name, vram, presets):
    return {"metadata": {"name": name, "id": "computeplatform-" + name}, "spec": {"gpuMemoryGibibytes": vram, "presets": presets}, "status": {}}


PLATFORMS = [
    _platform("gpu-h100-sxm", 80, [_preset("1gpu-16vcpu-200gb", 1, 16, 200), _preset("8gpu-128vcpu-1600gb", 8, 128, 1600)]),
    _platform("gpu-h200-sxm", 141, [_preset("1gpu-16vcpu-200gb", 1, 16, 200, 141)]),
    _platform("gpu-b200-sxm", 180, [_preset("8gpu-160vcpu-1792gb", 8, 160, 1792, 180)]),
    _platform("gpu-l40s-a", 48, [_preset("1gpu-8vcpu-32gb", 1, 8, 32, 48)]),                       # a GPU with no price in the table: left out
    _platform("cpu-d3", 0, [_preset("4vcpu-16gb", 0, 4, 16)]),                                      # no GPU: left out
]


class FakeNebius:
    """Just enough of https://api.nebius.cloud. `states` is what a new instance reports on each poll."""

    def __init__(self, states=("CREATING", "STARTING", "RUNNING"), region="eu-north1", platforms=None, subnets=None):
        self.calls, self.instances, self.n = [], {}, 0
        self.states, self.region = list(states), region
        self.platforms = platforms if platforms is not None else PLATFORMS
        self.subnets = subnets if subnets is not None else [{"metadata": {"id": "vpcsubnet-other"}, "status": {"default": False}},
                                                            {"metadata": {"id": SUBNET}, "status": {"default": True}}]
        self.fail = {}                              # (method, path-prefix) -> ApiError to raise
        self.operation = {}                         # fields added to the operation a create returns (e.g. a failure)
        self.page_size = 100

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        for (m, prefix), err in self.fail.items():
            if method == m and path.startswith(prefix):
                raise err
        route, _, qs = path.partition("?")
        query = dict(p.split("=", 1) for p in qs.split("&")) if qs else {}
        if method == "GET" and route == f"/iam/v2/projects/{PROJECT}":
            return {"metadata": {"id": PROJECT, "name": "default"}, "spec": {"region": self.region}, "status": {}}
        if method == "GET" and route == "/compute/v1/platforms":
            assert query["parentId"] == PROJECT
            return {"items": self.platforms}
        if method == "GET" and route == "/vpc/v1/subnets":
            assert query["parentId"] == PROJECT
            return {"items": self.subnets}
        if method == "POST" and route == "/compute/v1/instances":
            self.n += 1
            iid = f"computeinstance-{self.n}"
            self.instances[iid] = {"id": iid, "name": body["metadata"]["name"], "labels": body["metadata"].get("labels", {}), "polls": 0, "state": "CREATING", "body": body}
            return {"id": f"operation-{self.n}", "resourceId": iid, **self.operation}
        if method == "GET" and route == "/compute/v1/instances":
            rows = [self._view(i) for i in self.instances.values()]
            start = int(query.get("pageToken", "0"))
            page = rows[start:start + self.page_size]
            more = start + self.page_size < len(rows)
            return {"items": page, "nextPageToken": str(start + self.page_size) if more else ""}
        if method == "GET" and route.startswith("/compute/v1/instances/operations/"):
            return {"id": route.rsplit("/", 1)[1], "finishedAt": "2026-11-14T00:00:00Z", "status": {}}
        if method == "GET" and route.startswith("/compute/v1/instances/"):
            iid = route.rsplit("/", 1)[1]
            if iid not in self.instances:
                raise ApiError(404, json.dumps({"code": 5, "message": f"instance {iid} not found"}))
            i = self.instances[iid]
            if i["state"] != "DELETING":
                i["state"] = self.states[min(i["polls"], len(self.states) - 1)]
                i["polls"] += 1
            return self._view(i)
        if method == "DELETE" and route.startswith("/compute/v1/instances/"):
            iid = route.rsplit("/", 1)[1]
            if iid not in self.instances:
                raise ApiError(404, json.dumps({"code": 5, "message": f"instance {iid} not found"}))
            self.instances[iid]["state"] = "DELETING"
            return {"id": "operation-del", "resourceId": iid}
        raise AssertionError(f"unexpected call {method} {path}")

    @staticmethod
    def _view(i):
        running = i["state"] == "RUNNING"
        nic = {"name": "eth0", "ipAddress": {"address": "10.0.0.4/32"}}
        if running:
            nic["publicIpAddress"] = {"address": "203.0.113.9/32"}
        return {"metadata": {"id": i["id"], "name": i["name"], "labels": i["labels"], "parentId": PROJECT}, "spec": {}, "status": {"state": i["state"], "networkInterfaces": [nic]}}

    def creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/compute/v1/instances"]

    def deleted(self):
        return [c[1].rsplit("/", 1)[1] for c in self.calls if c[0] == "DELETE"]


def prov(api=None, **kw):
    return NebiusProvider(http=api or FakeNebius(), project_id=PROJECT, sleep=lambda s: None, **kw)


def spec(**kw):
    return HostSpec(**{"name": "labagent-t", "instance_type": "H100", "region": "eu-north1", "ssh_public_key": PUB1, "tags": ["labagent", host_tag("t")], **kw})


def pack_params(**kw):
    return {"cloud": "nebius", "instance_type": "H100", "region": "eu-north1", **kw}


def nrun(action, api, name="t", params=None, log=None, **extra):
    extra.setdefault("provider", lambda pid: prov(api))
    extra.setdefault("public_half", lambda path: PUB1)
    return run(action, api, name=name, params=params if params is not None else pack_params(ssh_key_file="/keys/mine"), log=log, **extra)


# ---- reading what Nebius describes ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("platform, want", [("gpu-h100-sxm", "H100"), ("gpu-h200-sxm", "H200"), ("gpu-b200-sxm", "B200"), ("gpu-l40s-a", "L40S"), ("cpu-d3", ""), ("", "")])
def test_the_gpu_family_comes_out_of_the_platform_name(platform, want):
    assert family_of(platform) == want


def test_a_region_says_where_it_is():
    assert [nebius_area(r) for r in ("eu-north1", "eu-west1", "us-central1", "uk-south1", "me-west1", "")] == ["europe", "europe", "north-america", "europe", "", ""]


def test_offers_are_priced_from_the_list_cheapest_first_and_leave_out_what_the_lab_cannot_price():
    offers = prov().offers(OfferQuery())
    assert [(o.instance_type, o.gpu_type, o.num_gpus, o.hourly_usd) for o in offers] == [
        ("gpu-h100-sxm/1gpu-16vcpu-200gb", "H100", 1, 4.50), ("gpu-h200-sxm/1gpu-16vcpu-200gb", "H200", 1, 5.40),
        ("gpu-h100-sxm/8gpu-128vcpu-1600gb", "H100", 8, 36.00), ("gpu-b200-sxm/8gpu-160vcpu-1792gb", "B200", 8, 68.00)]
    one = offers[0]
    assert (one.vendor, one.region, one.vram_gb, one.vcpus, one.memory_gb, one.interconnect, one.kind, one.area, one.available) == (
        "nebius", "eu-north1", 80, 16, 200, "SXM", "vm", "europe", True)
    assert "ubuntu24.04-cuda13.0" in one.os_options


def test_memory_comes_from_the_card_when_the_real_api_gives_none():
    """A real project's preset list had no gpuMemoryGibibytes at all (2026-11-14), which showed as 0G."""
    bare = [{"name": "gpu-h100-sxm", "metadata": {"name": "gpu-h100-sxm", "id": "computeplatform-h"}, "spec": {"presets": [{"name": "1gpu-16vcpu-200gb", "resources": {"gpuCount": 1, "vcpuCount": 16, "memoryGibibytes": 200}}]}, "status": {}},
            {"name": "gpu-b200-sxm", "metadata": {"name": "gpu-b200-sxm", "id": "computeplatform-b"}, "spec": {"presets": [{"name": "8gpu-160vcpu-1792gb", "resources": {"gpuCount": 8, "vcpuCount": 160, "memoryGibibytes": 1792}}]}, "status": {}}]
    offers = prov(FakeNebius(platforms=bare)).offers(OfferQuery())
    assert [(o.gpu_type, o.vram_gb) for o in offers] == [("H100", 80), ("B200", 0)]          # B200 stays unknown instead of a guess


def test_the_shape_other_clouds_use_works_here_too():
    p = prov()
    assert [o.instance_type for o in p.offers(OfferQuery(instance_type="H100"))] == ["gpu-h100-sxm/1gpu-16vcpu-200gb"]
    assert [o.instance_type for o in p.offers(OfferQuery(instance_type="H100x8"))] == ["gpu-h100-sxm/8gpu-128vcpu-1600gb"]
    assert [o.instance_type for o in p.offers(OfferQuery(instance_type="gpu-h200-sxm/1gpu-16vcpu-200gb"))] == ["gpu-h200-sxm/1gpu-16vcpu-200gb"]
    assert p.offers(OfferQuery(instance_type="A100")) == []


def test_filters_by_gpu_count_price_region_and_area():
    p = prov()
    assert {o.num_gpus for o in p.offers(OfferQuery(num_gpus=8))} == {8}
    assert [o.hourly_usd for o in p.offers(OfferQuery(max_price=5.0))] == [4.5]
    assert len(p.offers(OfferQuery(region="EU-NORTH1"))) == 4 and p.offers(OfferQuery(region="us-central1")) == []       # a project is in one region
    assert len(p.offers(OfferQuery(area="europe"))) == 4 and p.offers(OfferQuery(area="north-america")) == []
    assert p.offers(OfferQuery(vendor="scaleway")) == [] and len(p.offers(OfferQuery(vendor="nebius"))) == 4
    assert p.offers(OfferQuery(kind="baremetal")) == []


def test_the_region_is_read_from_the_project_once():
    api = FakeNebius(region="us-central1")
    p = prov(api)
    p.offers(OfferQuery())
    p.offers(OfferQuery())
    assert [c for c in api.calls if c[1].startswith("/iam/")] == [("GET", f"/iam/v2/projects/{PROJECT}", None)]
    assert p.offers(OfferQuery())[0].region == "us-central1" and p.offers(OfferQuery(area="north-america"))


def test_without_a_project_nothing_is_called_and_the_message_says_where_to_set_it(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.delenv("NEBIUS_PROJECT_ID", raising=False)
    api = FakeNebius()
    with pytest.raises(ComputeError, match="NEBIUS_PROJECT_ID"):
        NebiusProvider(http=api).offers(OfferQuery())
    assert api.calls == []
    monkeypatch.setenv("NEBIUS_PROJECT_ID", PROJECT)
    assert NebiusProvider(http=api).offers(OfferQuery())


def test_a_nebius_error_is_explained_with_its_message():
    api = FakeNebius()
    api.fail[("GET", "/compute/v1/platforms")] = ApiError(403, json.dumps({"code": 7, "message": "permission denied for compute.platforms.list"}))
    with pytest.raises(ComputeError, match="could not list the platforms.*permission denied for compute.platforms.list"):
        prov(api).offers(OfferQuery())
    api.fail[("GET", "/compute/v1/platforms")] = ApiError(401, "{}")
    with pytest.raises(ComputeError, match="rejected the token.*12 hours"):
        prov(api).offers(OfferQuery())


def test_there_is_no_ssh_key_list_and_the_message_says_how_the_key_gets_there():
    p = prov()
    for call in (p.ssh_keys, lambda: p.add_ssh_key("x", PUB1)):
        with pytest.raises(ComputeError, match="no SSH key list.*installed on each VM when it is created"):
            call()
    assert "ssh_keys" not in NebiusProvider.capabilities and NebiusProvider.key_registry is False and NebiusProvider.auto_delete is False


# ---- creating, finding and deleting a host --------------------------------------------------------------------------------

def test_validate_gives_the_price_and_names_what_is_offered_when_it_is_not():
    info = prov().validate(spec())
    assert info == {"hourly_cents": 450, "os_options": list(nebius.IMAGES), "gpu_type": "H100", "instance_type": "gpu-h100-sxm/1gpu-16vcpu-200gb"}
    with pytest.raises(ComputeError, match=r"A100 is not offered in the Nebius project's region eu-north1\. Offered there: gpu-h100-sxm/1gpu-16vcpu-200gb \$4\.50/h"):
        prov().validate(spec(instance_type="A100"))


@pytest.mark.parametrize("patch, why", [
    ({"name": "bad name!"}, "host name must be"),
    ({"ssh_public_key": None}, "a public key is needed"),
    ({"ssh_public_key": "not a key"}, "a public key is needed"),
    ({"region": "us-central1"}, "is in eu-north1, not us-central1"),
    ({"os_image": "Bad Image!"}, "does not look like a Nebius image family"),
])
def test_a_launch_that_cannot_work_is_refused_before_anything_is_created(patch, why):
    api = FakeNebius()
    with pytest.raises(ComputeError, match=why):
        prov(api).validate(spec(**patch))
    assert api.creates() == []


def test_create_sends_the_documented_request_with_the_key_in_cloud_init_and_nothing_preemptible():
    api = FakeNebius()
    hid = prov(api).create(spec(tags=["labagent", host_tag("t")]))
    assert hid == "computeinstance-1"
    body = api.creates()[0][2]
    assert body["metadata"]["parentId"] == PROJECT and body["metadata"]["name"] == "labagent-t"
    assert set(body["metadata"]["labels"]) == {"labagent-0", "labagent-1"} and body["metadata"]["labels"]["labagent-1"] == "labagent-gpu-notebooks-t"
    s = body["spec"]
    assert s["resources"] == {"platform": "gpu-h100-sxm", "preset": "1gpu-16vcpu-200gb"}
    assert s["networkInterfaces"] == [{"name": "eth0", "subnetId": SUBNET, "ipAddress": {}, "publicIpAddress": {}}]      # the project's DEFAULT subnet, a public address
    disk = s["bootDisk"]["managedDisk"]
    assert s["bootDisk"]["attachMode"] == "READ_WRITE" and disk["spec"]["type"] == "NETWORK_SSD" and disk["spec"]["sizeGibibytes"] == "300"
    assert disk["spec"]["sourceImageFamily"] == {"imageFamily": "ubuntu24.04-cuda13.0"} and disk["name"] == "labagent-t-boot"
    assert s["recoveryPolicy"] == "FAIL" and s["reservationPolicy"] == {"policy": "FORBID"}
    assert "preemptible" not in s and "followsSpotPrice" not in s and "securityGroups" not in s["networkInterfaces"][0]
    init = s["cloudInitUserData"]
    assert init.startswith("#cloud-config") and "name: ubuntu" in init and json.dumps(" ".join(PUB1.split()[:3])) in init
    assert "root" not in init.replace("lock_passwd", "") and "lock_passwd: true" in init


def test_an_image_family_can_be_chosen():
    api = FakeNebius()
    prov(api).create(spec(os_image="ubuntu24.04-driverless"))
    assert api.creates()[0][2]["spec"]["bootDisk"]["managedDisk"]["spec"]["sourceImageFamily"] == {"imageFamily": "ubuntu24.04-driverless"}


def test_the_subnet_can_be_chosen_and_a_project_without_one_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("NEBIUS_SUBNET_ID", "vpcsubnet-mine")
    api = FakeNebius()
    prov(api).create(spec())
    assert api.creates()[0][2]["spec"]["networkInterfaces"][0]["subnetId"] == "vpcsubnet-mine"
    assert not [c for c in api.calls if c[1].startswith("/vpc/")]
    monkeypatch.delenv("NEBIUS_SUBNET_ID")
    empty = FakeNebius(subnets=[])
    with pytest.raises(ComputeError, match="no network subnet.*create-default"):
        prov(empty).create(spec())
    assert empty.creates() == []


def test_a_create_that_nebius_fails_at_once_says_why():
    api = FakeNebius()
    api.operation = {"finishedAt": "2026-11-14T00:00:00Z", "status": {"code": 8, "message": "quota exceeded: gpu-h100-sxm, requested 1, available 0"}}
    with pytest.raises(ComputeError, match="could not create the instance: quota exceeded"):
        prov(api).create(spec())
    api2 = FakeNebius()
    api2.fail[("POST", "/compute/v1/instances")] = ApiError(400, json.dumps({"code": 3, "message": "invalid image family"}))
    with pytest.raises(ComputeError, match="could not create the instance.*invalid image family"):
        prov(api2).create(spec())


def test_a_create_still_running_is_not_waited_for_beyond_a_few_seconds():
    api, naps = FakeNebius(), []
    p = NebiusProvider(http=api, project_id=PROJECT, sleep=naps.append)
    assert p.create(spec()) == "computeinstance-1"
    assert sum(naps) <= nebius.OPERATION_WAIT_S + 2 and api.creates()


def test_a_create_that_answers_with_no_id_adopts_the_vm_by_name_or_reports_a_possible_stray():
    api = FakeNebius()
    real = api.__call__

    def no_id(method, path, body=None):
        out = real(method, path, body)
        if method == "POST":
            out.pop("resourceId", None)
        return out
    p = NebiusProvider(http=no_id, project_id=PROJECT, sleep=lambda s: None)
    assert p.create(spec()) == "computeinstance-1"                                              # found again by its name and tag
    lost = NebiusProvider(http=lambda m, path, body=None: {"id": "op"} if m == "POST" else real(m, path, body), project_id=PROJECT, sleep=lambda s: None)
    with pytest.raises(ComputeError, match="returned no instance id: check the Nebius console"):
        lost.create(spec(name="labagent-other"))


def test_find_needs_the_exact_name_and_tag_follows_pages_and_skips_the_dead():
    api = FakeNebius(states=("RUNNING",))
    p = prov(api)
    p.create(spec())
    api.page_size = 1
    p.create(spec(name="labagent-u", tags=["labagent", host_tag("u")]))
    assert p.find("labagent-t", host_tag("t")).id == "computeinstance-1"
    assert p.find("labagent-u", host_tag("u")).id == "computeinstance-2"
    assert p.find("labagent-t", host_tag("u")) is None and p.find("labagent-x", host_tag("t")) is None
    api.instances["computeinstance-1"]["state"] = "DELETING"
    assert p.find("labagent-t", host_tag("t")) is None


def test_a_long_tag_is_cut_to_a_label_and_still_found_again():
    api = FakeNebius(states=("RUNNING",))
    p = prov(api)
    tag = "labagent:gpu-notebooks:" + "x" * 100
    p.create(spec(tags=["labagent", tag]))
    assert all(len(v) <= nebius.TAG_MAX for v in api.instances["computeinstance-1"]["labels"].values())
    assert p.find("labagent-t", tag).id == "computeinstance-1"


def test_get_maps_states_and_strips_the_prefix_length_off_the_address():
    api = FakeNebius(states=("CREATING", "STARTING", "RUNNING", "STOPPED", "ERROR"))
    p = prov(api)
    hid = p.create(spec())
    seen = [p.get(hid) for _ in range(5)]
    assert [h.status for h in seen] == ["pending", "pending", "active", "stopped", "error"]
    assert seen[2].ip == "203.0.113.9" and seen[0].ip is None and (seen[2].ssh_user, seen[2].ssh_port) == ("ubuntu", 22)
    assert p.get("computeinstance-99") is None


def test_delete_deletes_the_instance_and_its_boot_disk_goes_with_it_and_an_already_gone_instance_is_fine():
    api = FakeNebius()
    p = prov(api)
    hid = p.create(spec())
    p.delete(hid)
    assert api.deleted() == [hid] and p.get(hid).status == "deleting"
    p.delete("computeinstance-99")                                                              # 404: nothing to do
    api.fail[("DELETE", "/compute/v1/instances/")] = ApiError(500, json.dumps({"code": 13, "message": "internal"}))
    with pytest.raises(ComputeError, match="could not delete the instance.*internal"):
        p.delete(hid)


# ---- where the login comes from -----------------------------------------------------------------------------------------------

@pytest.fixture
def nhome(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    for var in ("NEBIUS_IAM_TOKEN", "NEBIUS_PROJECT_ID", "NEBIUS_SUBNET_ID", "NEBIUS_SERVICE_ACCOUNT_FILE", "LAMBDA_API_KEY", "SHADEFORM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(nebius.shutil, "which", lambda name: None)                              # no nebius command line tool unless a test adds one
    monkeypatch.setattr(nebius, "_installed_cli", lambda: None)                                # nor one in ~/.nebius/bin of the machine running the tests
    (tmp_path / "keys").mkdir()
    return tmp_path


def test_the_token_comes_from_the_environment_then_the_credentials_file_then_the_key_file(nhome, monkeypatch):
    (nhome / "keys" / "nebius_iam_token").write_text("file-token\n")
    assert Auth().token() == "file-token"
    (nhome / "credentials").write_text("[nebius]\ncredentials-token\n")
    assert Auth().token() == "credentials-token"
    monkeypatch.setenv("NEBIUS_IAM_TOKEN", "env-token")
    assert Auth().token() == "env-token"
    assert Auth(token="given").token() == "given"


def test_the_project_comes_from_the_environment_or_a_file(nhome, monkeypatch):
    api = FakeNebius()
    (nhome / "keys" / "nebius_project_id").write_text(PROJECT + "\n")
    assert NebiusProvider(http=api).offers(OfferQuery())


def test_with_no_login_at_all_the_message_gives_the_three_ways(nhome):
    with pytest.raises(ComputeError, match="NEBIUS_IAM_TOKEN.*service account.*nebius.*command line tool"):
        NebiusProvider(project_id=PROJECT)


def test_the_nebius_command_line_tool_is_used_when_nothing_else_is_set(nhome, monkeypatch):
    monkeypatch.setattr(nebius.shutil, "which", lambda name: "/usr/local/bin/nebius")
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return SimpleNamespace(returncode=0, stdout="cli-token\n", stderr="")
    auth = Auth(run=fake_run)
    assert auth.token() == "cli-token" and auth.token() == "cli-token" and ran == [["/usr/local/bin/nebius", "iam", "get-access-token"]]      # asked once, kept, by the path that was found
    bad = Auth(run=lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="not logged in"))
    with pytest.raises(ComputeError, match="gave no token"):
        bad.token()


def test_a_service_account_signs_a_five_minute_jwt_and_trades_it_for_a_token(nhome):
    key = nhome / "keys" / "sa.pem"
    key.write_text("pretend PEM")
    (nhome / "keys" / "nebius_service_account.json").write_text(json.dumps({"service_account_id": "serviceaccount-abc", "public_key_id": "publickey-xyz", "private_key_file": "sa.pem"}))
    signed = []

    def fake_run(argv, input=None, **kw):
        signed.append((argv, input))
        return SimpleNamespace(returncode=0, stdout=b"\x01\x02signature", stderr=b"")
    exchanged = []

    def fake_exchange(jwt):
        exchanged.append(jwt)
        return {"access_token": "sa-token", "expires_in": 43200}
    now = [1_000_000.0]
    auth = Auth(run=fake_run, exchange=fake_exchange, clock=lambda: now[0])
    assert auth.token() == "sa-token" and auth.token() == "sa-token" and len(exchanged) == 1       # cached
    argv, data = signed[0]
    assert argv == ["openssl", "dgst", "-sha256", "-sign", str(key)]
    head, body, sig = exchanged[0].split(".")
    import base64
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT", "kid": "publickey-xyz"}
    claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    assert claims == {"iss": "serviceaccount-abc", "sub": "serviceaccount-abc", "exp": 1_000_300}
    assert data == f"{head}.{body}".encode() and base64.urlsafe_b64decode(pad(sig)) == b"\x01\x02signature"
    now[0] += 43200 - 30                                                                           # about to run out: renewed
    assert auth.token() == "sa-token" and len(exchanged) == 2


def test_a_broken_service_account_file_says_what_is_wrong_without_showing_it(nhome):
    f = nhome / "keys" / "nebius_service_account.json"
    f.write_text(json.dumps({"service_account_id": "serviceaccount-abc"}))
    with pytest.raises(ComputeError, match="missing: public_key_id, private_key_file"):
        Auth().token()
    f.write_text("{not json")
    with pytest.raises(ComputeError, match="is not a JSON file"):
        Auth().token()


def test_signing_works_with_a_real_openssl_key_when_openssl_is_there(tmp_path):
    import shutil
    if not shutil.which("openssl"):
        pytest.skip("no openssl")
    pem = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(pem), "2048"], check=True, capture_output=True)
    jwt = sign_jwt({"service_account_id": "serviceaccount-a", "public_key_id": "publickey-b", "private_key_file": str(pem)}, 1_000_000)
    assert jwt.count(".") == 2 and len(jwt.split(".")[2]) > 300                                      # a 2048 bit RSA signature is 256 bytes
    with pytest.raises(ComputeError, match="could not sign"):
        sign_jwt({"service_account_id": "a", "public_key_id": "b", "private_key_file": str(tmp_path / "missing.pem")}, 1)


def test_a_token_exchange_that_nebius_refuses_says_so_without_the_jwt(monkeypatch):
    import urllib.error

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, None)
    monkeypatch.setattr(nebius.urllib.request, "urlopen", refuse)
    with pytest.raises(ComputeError, match="refused the service account \\(HTTP 400\\)") as e:
        nebius.exchange_jwt("secret.jwt.value")
    assert "secret.jwt.value" not in str(e.value)


def test_the_cloud_list_shows_nebius_and_counts_a_service_account_or_the_cli_as_a_login(nhome, monkeypatch):
    rows = {c["id"]: c for c in catalog()}
    assert rows["nebius"]["credentials"]["configured"] is False and rows["nebius"]["credentials"]["env_var"] == "NEBIUS_IAM_TOKEN"
    assert rows["nebius"]["credentials"]["key_file"].endswith("nebius_iam_token") and rows["nebius"]["capabilities"] == ["offers", "launch"]
    (nhome / "keys" / "nebius_service_account.json").write_text("{}")
    c = {x["id"]: x for x in catalog()}["nebius"]["credentials"]
    assert c["configured"] is True and c["source"] == "service account file"
    (nhome / "keys" / "nebius_service_account.json").unlink()
    monkeypatch.setattr(nebius.shutil, "which", lambda name: "/usr/bin/nebius")
    (nhome.parent / ".nebius").mkdir(exist_ok=True)
    monkeypatch.setattr(nebius.Path, "home", classmethod(lambda cls: nhome.parent))
    assert {x["id"]: x for x in catalog()}["nebius"]["credentials"]["source"] == "nebius CLI"


# ---- the pack on Nebius ---------------------------------------------------------------------------------------------------------

def test_a_launch_on_nebius_installs_the_key_at_creation_logs_in_as_ubuntu_and_says_the_vm_is_not_deleted_for_you(home):
    api, ssh, lines = FakeNebius(), FakeSsh(), []
    env = nrun("launch", api, ssh=ssh, log=lines.append)
    assert env.status == "ok", env.to_dict()
    out = env.summary["outputs"]
    assert (out["cloud"], out["host_id"], out["ip"], out["ssh_user"], out["hourly_cents"], out["auto_delete"]) == ("nebius", "computeinstance-1", "203.0.113.9", "ubuntu", 450, False)
    assert (out["ssh_key_id"], out["ssh_key_name"], out["ssh_key_fingerprint"], out["ssh_key_verified"]) == ("", "installed at creation", FP1, True)
    assert ssh.args[:3] == ("203.0.113.9", "ubuntu", 22)
    text = "\n".join(lines)
    assert "NOT deleted by nebius" in text and "bills until you run `labagent destroy gpu-notebooks --name t`" in text
    assert json.dumps(" ".join(PUB1.split()[:3])) in api.creates()[0][2]["spec"]["cloudInitUserData"] and api.deleted() == []
    assert load_state("gpu-notebooks", "t")["ssh_public_key"] == PUB1                                  # a public key is not a secret, and a rerun asks for the same one


def test_an_ssh_key_id_is_ignored_on_nebius_with_a_warning(home):
    lines = []
    env = nrun("launch", FakeNebius(), params=pack_params(ssh_key_file="/keys/mine", ssh_key_id="k1"), log=lines.append)
    assert env.status == "ok", env.to_dict()
    assert any("ssh_key_id is ignored on nebius" in l for l in lines)


def test_without_a_key_file_nothing_is_created_and_the_message_says_how_to_make_one(home, monkeypatch):
    monkeypatch.delenv("LABAGENT_SSH_KEY_FILE", raising=False)
    api = FakeNebius()
    env = nrun("launch", api, params=pack_params())
    assert env.status == "failed" and "no SSH key to log in with" in env.error and "installs its public half on the VM" in env.error
    assert api.creates() == []


def test_location_picks_the_project_region_on_nebius_and_a_wrong_area_finds_nothing(home):
    api = FakeNebius()
    env = nrun("launch", api, params={"cloud": "nebius", "instance_type": "H100", "location": "europe", "ssh_key_file": "/keys/mine"})
    assert env.status == "ok", env.to_dict()
    assert env.summary["outputs"]["region"] == "eu-north1" and load_state("gpu-notebooks", "t")["vendor"] == "nebius"
    elsewhere = nrun("launch", FakeNebius(), name="u", params={"cloud": "nebius", "instance_type": "H100", "location": "north-america", "ssh_key_file": "/keys/mine"})
    assert elsewhere.status == "failed" and "no H100 is available in North America" in elsewhere.error


def test_a_launch_that_fails_deletes_the_nebius_vm_since_nebius_will_not(home):
    api = FakeNebius()
    env = nrun("launch", api, ssh=FakeSsh(down_for=10 ** 6), clock=ticks())
    assert env.status == "failed" and "cannot log in to the VM over ssh" in env.error
    assert api.deleted() == ["computeinstance-1"] and env.summary["cleanup"] == {"action": "deleted", "host_id": "computeinstance-1"}
    assert not state_path("gpu-notebooks", "t").exists()


def test_a_create_that_nebius_fails_at_once_creates_nothing_and_says_why(home):
    api = FakeNebius()
    api.operation = {"finishedAt": "2026-11-14T00:00:00Z", "status": {"code": 8, "message": "no capacity for gpu-h100-sxm"}}
    env = nrun("launch", api)
    assert env.status == "failed" and "no capacity for gpu-h100-sxm" in env.error


def test_a_rerun_after_success_creates_nothing_more_and_destroy_deletes_only_this_one(home):
    api = FakeNebius()
    assert nrun("launch", api).status == "ok"
    again = nrun("launch", api)
    assert again.status == "ok" and len(api.creates()) == 1
    assert nrun("destroy", api).status == "ok"
    assert api.deleted() == ["computeinstance-1"] and not state_path("gpu-notebooks", "t").exists()


def test_destroy_after_a_lost_state_file_finds_the_vm_by_name_and_tag(home):
    api = FakeNebius(states=("RUNNING",))
    prov(api).create(spec(name=host_name("t")))
    assert nrun("destroy", api).status == "ok"
    assert api.deleted() == ["computeinstance-1"]


def test_the_token_never_reaches_state_envelope_logs_or_errors(home, monkeypatch):
    secret = "nebius-secret-token-0123456789"
    monkeypatch.setenv("NEBIUS_IAM_TOKEN", secret)
    monkeypatch.setenv("NEBIUS_PROJECT_ID", PROJECT)
    seen = []
    api = FakeNebius()

    def spying_http(auth):
        def call(method, path, body=None):
            seen.append(auth.token())                                                                  # the token is used here and nowhere else
            return api(method, path, body)
        return call
    monkeypatch.setattr(nebius, "make_http", spying_http)
    ssh, lines = FakeSsh(), []
    env = run("launch", None, name="t", params=pack_params(ssh_key_file="/keys/mine"), log=lines.append, ssh=ssh, provider=providers.make, public_half=lambda p: PUB1)
    assert env.status == "ok", env.to_dict()
    assert seen and set(seen) == {secret}
    blob = json.dumps(env.to_dict()) + json.dumps(load_state("gpu-notebooks", "t")) + "".join(ssh.commands()) + "".join(lines)
    assert secret not in blob


def test_the_pack_offers_nebius_as_a_platform_and_names_its_shapes_and_region():
    pack = GpuNotebooks()
    assert pack.platforms == ("shadeform", "lambda", "nebius")
    params = {p.name: p for p in pack.params}
    assert "nebius" in params["cloud"].help and "gpu-h100-sxm/1gpu-16vcpu-200gb" in params["instance_type"].help
    assert "NEBIUS_PROJECT_ID" in params["region"].help and "not used on nebius" in params["ssh_key_id"].help.lower() and "lambda and nebius" in params["hours"].help
