"""Nebius Devlabs (containers) as a cloud, against a fake API. The routes and JSON shapes were checked on 2026-11-22 against Nebius's OpenAPI file and
with `dryRun` create calls on a real project (nothing created). No real Devlab has been created by these tests: they prove nothing about a running one.
"""
import csv
import json

import pytest

from labagent.compute import providers
from labagent.compute.nebius import NebiusProvider
from labagent.compute.nebius_devlab import DISK_GIB, TEMPLATE_JUPYTER, NebiusDevlabProvider
from labagent.compute.errors import ComputeError
from labagent.compute.providers import ApiError, HostSpec, OfferQuery
from labagent.packs.base import run_pack, state_path
from labagent.packs.gpu_notebooks.container import GpuNotebooksContainer
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, credentials_path
from tests.test_gpu_nebius import PROJECT, FakeNebius
from tests.test_gpu_notebooks import PUB1, home  # noqa: F401  (home is a fixture)
from tests.test_gpu_ssh_keys import ticks


class FakeDevlabs(FakeNebius):
    """The project and platform routes of FakeNebius, plus /ai/v1/devlabs."""

    def __init__(self, states=("PROVISIONING", "IMAGE_PULLING", "RUNNING"), endpoints=True, **kw):
        super().__init__(**kw)
        self.devlabs, self.dn, self.dstates, self.endpoints = {}, 0, list(states), endpoints

    def __call__(self, method, path, body=None):
        route = path.partition("?")[0]
        if not route.startswith("/ai/v1/devlabs"):
            return super().__call__(method, path, body)
        self.calls.append((method, path, body))
        if method == "POST" and route == "/ai/v1/devlabs":
            self.dn += 1
            did = f"aidevlab-{self.dn}"
            self.devlabs[did] = {"name": body["metadata"]["name"], "labels": body["metadata"].get("labels", {}), "polls": 0, "state": "PROVISIONING", "body": body}
            return {"id": f"operation-{self.dn}", "resourceId": did, "finishedAt": "2026-11-22T00:00:00Z", "status": {}}
        if method == "GET" and route == "/ai/v1/devlabs":
            return {"items": [self._d(k) for k in self.devlabs], "nextPageToken": ""}
        did = route.rsplit("/", 1)[1]
        if did not in self.devlabs:
            raise ApiError(404, json.dumps({"code": 5, "message": f"devlab {did} not found"}))
        d = self.devlabs[did]
        if method == "GET":
            if d["state"] != "DELETING":
                d["state"] = self.dstates[min(d["polls"], len(self.dstates) - 1)]
                d["polls"] += 1
            return self._d(did)
        if method == "DELETE":
            d["state"] = "DELETING"
            return {"id": "operation-del", "resourceId": did}
        raise AssertionError(f"unexpected call {method} {path}")

    def _d(self, did):
        d = self.devlabs[did]
        urls = [f"https://{did}.devlab.example.nebius.cloud/"] if d["state"] == "RUNNING" and self.endpoints else []
        return {"metadata": {"id": did, "name": d["name"], "labels": d["labels"], "parentId": PROJECT}, "spec": {},
                "status": {"state": d["state"], "publicEndpoints": urls}}

    def dcreates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/ai/v1/devlabs"]

    def ddeleted(self):
        return [c[1].rsplit("/", 1)[1] for c in self.calls if c[0] == "DELETE" and c[1].startswith("/ai/v1/devlabs/")]


def prov(api):
    return NebiusDevlabProvider(http=api, project_id=PROJECT, sleep=lambda s: None)


# ---- the provider ----

def test_offers_are_containers_from_the_same_platforms_and_the_vm_cloud_does_not_list_them():
    api = FakeDevlabs()
    got = prov(api).offers(OfferQuery(gpu_type="H100"))
    assert [(o.kind, o.vendor, o.instance_type, o.num_gpus) for o in got] == [("container", "nebius-devlab", "gpu-h100-sxm/1gpu-16vcpu-200gb", 1),
                                                                               ("container", "nebius-devlab", "gpu-h100-sxm/8gpu-128vcpu-1600gb", 8)]
    assert got[0].storage_gb == DISK_GIB and got[0].os_options == []
    vm = NebiusProvider(http=api, project_id=PROJECT).offers(OfferQuery(gpu_type="H100"))
    assert {o.kind for o in vm} == {"vm"} and {o.vendor for o in vm} == {"nebius"}
    assert prov(api).offers(OfferQuery(kind="vm")) == [] and NebiusProvider(http=api, project_id=PROJECT).offers(OfferQuery(kind="container")) == []


def test_create_sends_the_jupyter_template_one_gpu_a_big_enough_disk_and_the_token():
    api = FakeDevlabs()
    spec = HostSpec(name="labagent-t", instance_type="H100", region="eu-north1", tags=["labagent", "t-tag"], jupyter_password="abcDEF1234567890")
    hid = prov(api).create(spec)
    (_, path, body), = api.dcreates()
    assert hid == "aidevlab-1" and path == "/ai/v1/devlabs" and "dryRun" not in body
    s = body["spec"]
    assert body["metadata"]["parentId"] == PROJECT and body["metadata"]["name"] == "labagent-t"
    assert s["template"] == {"id": TEMPLATE_JUPYTER, "inputFieldValues": {"jupyter_token": "abcDEF1234567890"}}
    assert s["gpu"] == ["H100:1"] and s["disk"] == {"sizeBytes": str(DISK_GIB * 1024 ** 3), "type": "NETWORK_SSD"} and DISK_GIB >= 90
    assert "platform" not in s and "sshAuthorizedKeys" not in s


@pytest.mark.parametrize("token", [None, "", "short", "has-dash-1234567", "x" * 17])
def test_a_token_the_template_would_refuse_is_refused_before_any_call(token):
    api = FakeDevlabs()
    with pytest.raises(ComputeError, match="16 letters and digits"):
        prov(api).create(HostSpec(name="x", instance_type="H100", jupyter_password=token))
    assert api.dcreates() == []


def test_the_state_maps_and_a_devlab_is_active_only_when_its_address_is_ready():
    api = FakeDevlabs(endpoints=False)
    p = prov(api)
    hid = p.create(HostSpec(name="x", instance_type="H100", jupyter_password="abcDEF1234567890"))
    assert [p.get(hid).status for _ in range(3)] == ["pending", "pending", "pending"]        # RUNNING but no address yet
    api2 = FakeDevlabs()
    p2 = prov(api2)
    hid = p2.create(HostSpec(name="x", instance_type="H100", jupyter_password="abcDEF1234567890"))
    seen = [p2.get(hid) for _ in range(3)]
    assert [h.status for h in seen] == ["pending", "pending", "active"]
    assert seen[-1].url == "https://aidevlab-1.devlab.example.nebius.cloud" and seen[-1].ip is None
    assert p2.web_url(hid, 8888) == seen[-1].url and p2.get("aidevlab-99") is None


def test_validate_refuses_an_image_a_wrong_region_and_an_unknown_shape():
    p = prov(FakeDevlabs())
    with pytest.raises(ComputeError, match="takes no image"):
        p.validate(HostSpec(name="x", instance_type="H100", os_image="ubuntu24.04-cuda13.0"))
    with pytest.raises(ComputeError, match="is in eu-north1"):
        p.validate(HostSpec(name="x", instance_type="H100", region="us-central1"))
    with pytest.raises(ComputeError, match="not offered"):
        p.validate(HostSpec(name="x", instance_type="B300"))
    assert p.validate(HostSpec(name="x", instance_type="H100"))["hourly_cents"] == 450


def test_find_matches_name_and_label_and_delete_is_idempotent():
    api = FakeDevlabs()
    p = prov(api)
    hid = p.create(HostSpec(name="labagent-t", instance_type="H100", tags=["labagent", "t-tag"], jupyter_password="abcDEF1234567890"))
    assert p.find("labagent-t", "t-tag").id == hid and p.find("labagent-t", "other") is None and p.find("nope", "t-tag") is None
    p.delete(hid)
    p.delete(hid)
    p.delete("aidevlab-99")                                  # a 404 is "already gone", not an error
    assert api.ddeleted()[0] == hid


# ---- the container pack on it ----

class NoSsh:
    def __call__(self, *a):
        raise AssertionError("a Devlab has no ssh: the pack must not log in")


def go(action, api, p=None, log=None, **extra):
    services = {"provider": lambda pid: prov(api), "sleep": lambda s: None, "ssh": NoSsh(), "http_get": lambda url, verify: 200, "clock": ticks(), **extra}
    return run_pack(GpuNotebooksContainer(), action, "t", p or {"cloud": "nebius", "instance_type": "H100", "region": "eu-north1"}, services=services, log=log)


def test_launch_on_nebius_makes_one_devlab_with_one_jupyter_and_reports_its_url_and_token(home):  # noqa: F811
    api, lines = FakeDevlabs(), []
    env = go("launch", api, log=lines.append)
    assert env.status == "ok", env.to_dict()
    assert len(api.dcreates()) == 1 and api.creates() == []                                  # a Devlab, never a VM
    row = list(csv.DictReader(credentials_path("t").open()))
    assert len(row) == 1                                                                     # ONE JupyterLab
    token = row[0]["token"]
    assert len(token) == 16 and token.isalnum() and api.dcreates()[0][2]["spec"]["template"]["inputFieldValues"]["jupyter_token"] == token
    assert row[0]["url"] == "https://aidevlab-1.devlab.example.nebius.cloud"
    text = "\n".join(lines)
    assert f"URL       {row[0]['url']}" in text and f"Password  {token}" in text and "No ssh address" in text and "sign in to Nebius" in text
    assert "destroy gpu-notebooks-container --name t" in text and "NOT deleted automatically" in text
    assert state_path("gpu-notebooks-container", "t").read_text().count(token) == 0


def test_without_a_key_the_devlab_has_no_ssh_and_the_launch_still_works(home):  # noqa: F811
    api = FakeDevlabs()
    assert go("launch", api).status == "ok"
    spec = api.dcreates()[0][2]["spec"]
    assert "sshAuthorizedKeys" not in spec and "publicIp" not in spec


def test_with_a_key_file_the_shared_key_is_installed_and_a_public_ip_is_asked_for(home):  # noqa: F811
    api, lines = FakeDevlabs(), []
    p = {"cloud": "nebius", "instance_type": "H100", "region": "eu-north1", "ssh_key_file": "/keys/mine"}
    env = go("launch", api, p=p, log=lines.append, public_half=lambda path: PUB1)
    assert env.status == "ok", env.to_dict()
    spec = api.dcreates()[0][2]["spec"]
    assert spec["sshAuthorizedKeys"] == [" ".join(PUB1.split()[:3])] and spec["publicIp"] is True
    assert "installed on the VM when nebius-devlab creates it" in "\n".join(lines)


def test_a_devlab_with_a_public_ip_reports_the_shell_line_as_untested():
    h = NebiusDevlabProvider._host({"metadata": {"id": "d1", "name": "n"}, "status": {"state": "RUNNING", "publicEndpoints": ["https://x/"],
                                                                                    "instances": [{"publicIp": "203.0.113.9"}]}})
    assert h.ip == "203.0.113.9" and h.url == "https://x" and h.status == "active"


def test_nebius_devlab_is_accepted_by_its_own_name_and_a_rerun_creates_nothing_new(home):  # noqa: F811
    api = FakeDevlabs()
    p = {"cloud": "nebius-devlab", "instance_type": "H100", "region": "eu-north1"}
    assert go("launch", api, p=p).status == "ok" and go("launch", api, p=p).status == "ok"
    assert len(api.dcreates()) == 1


def test_a_failed_launch_deletes_the_devlab(home):  # noqa: F811
    api = FakeDevlabs(states=("PROVISIONING", "FAILED"))
    env = go("launch", api)
    assert env.status == "failed" and api.ddeleted() == ["aidevlab-1"]


def test_destroy_removes_the_devlab_and_the_token_file(home):  # noqa: F811
    api = FakeDevlabs()
    assert go("launch", api).status == "ok" and credentials_path("t").exists()
    assert go("destroy", api).status == "ok" and api.ddeleted() == ["aidevlab-1"] and not credentials_path("t").exists()


def test_an_image_is_refused_for_a_devlab(home):  # noqa: F811
    api = FakeDevlabs()
    env = go("launch", api, p={"cloud": "nebius", "instance_type": "H100", "region": "eu-north1", "os": "docker.io/library/nginx"})
    assert env.status == "failed" and "takes no image" in str(env.to_dict()) and api.dcreates() == []


# ---- the many-labs pack stays on VMs ----

def test_gpu_notebooks_never_uses_a_devlab(home):  # noqa: F811
    api = FakeDevlabs()
    env = run_pack(GpuNotebooks(), "launch", "t", {"cloud": "nebius-devlab", "instance_type": "H100", "location": "any", "mode": "mig", "ssh_key_file": "/k"},
                   services={"provider": lambda pid: prov(api), "sleep": lambda s: None, "public_half": lambda p: PUB1, "private_fingerprint": lambda p: "x"})
    assert env.status == "failed" and api.dcreates() == []


def test_the_vm_pack_does_not_ask_container_only_clouds_in_auto_mode():
    assert set(GpuNotebooks()._auto_clouds()) == {"lambda", "nebius", "shadeform"}
    assert set(GpuNotebooksContainer()._auto_clouds()) == {"runpod", "nebius-devlab"}


def test_registered_and_listed_as_a_container_cloud():
    assert providers.PROVIDERS["nebius-devlab"] is NebiusDevlabProvider
    assert NebiusDevlabProvider.container_only and NebiusDevlabProvider.builtin_jupyter and not NebiusDevlabProvider.has_ssh


def test_the_offers_hint_for_a_container_names_only_parameters_the_container_pack_has(capsys):
    from labagent.compute import cli as compute_cli
    from labagent.packs import registry
    o = prov(FakeDevlabs()).offers(OfferQuery(gpu_type="H100"))
    text = compute_cli.offers_text("nebius-devlab", o, len(o), False)
    line = next(l for l in text.splitlines() if l.strip().startswith("labagent launch gpu-notebooks-container"))
    known = {p.name for p in registry.resolve("gpu-notebooks-container").params}
    given = {kv.split("=", 1)[0] for kv in line.split("--param ")[1:]}
    assert given <= known and "labs" not in line and "one JupyterLab" in text
