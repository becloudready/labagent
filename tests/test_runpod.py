"""RunPod as a compute provider, against a fake RunPod API v2 (no network, no key, no cost).

The fake answers with the shapes in RunPod's published API reference (checked 2026-11-19): `{"gpus": [...]}` with `price`, `maxCount` and `dataCenters`,
SSH keys as plain strings under `keys`, pods with `status`, `ssh.direct` and `env`, list pages with `pagination`, and problem+json errors.
No real pod was launched by these tests.
"""
import json
import re

import pytest

from labagent.compute import providers
from labagent.compute.errors import ComputeError
from labagent.compute.providers import ApiError, HostSpec, OfferQuery, catalog, find_secret
from labagent.compute.runpod import DEFAULT_IMAGE, RunpodProvider, family_of, runpod_area
from tests.test_gpu_notebooks import FP1, PUB1, PUB2, home  # noqa: F401  (home is a fixture)

KEY = "rp-test-key-0123456789"


def _gpu(gid, name, mem, secure, community, max_secure=8, max_community=4, dcs=(("US-KS-2", "US Kansas 2", "HIGH"),), pool="X"):
    return {"id": gid, "name": name, "pool": pool, "manufacturer": "NVIDIA", "memory": mem, "secure": secure is not None, "community": community is not None,
            "price": {"secure": secure or 0, "community": community or 0}, "maxCount": {"secure": max_secure if secure else 0, "community": max_community if community else 0},
            "availability": dcs[0][2] if dcs else "NONE", "dataCenters": [{"id": i, "name": n, "availability": a} for i, n, a in dcs]}


GPUS = [
    _gpu("NVIDIA GeForce RTX 4090", "RTX 4090", 24, 0.44, 0.31),
    _gpu("NVIDIA H100 80GB HBM3", "H100 SXM", 80, 2.69, None, dcs=(("US-KS-2", "US Kansas 2", "LOW"), ("EU-RO-1", "EU Romania 1", "HIGH"), ("AP-JP-1", "AP Japan 1", "NONE"))),
    _gpu("NVIDIA A100 80GB PCIe", "A100 PCIe", 80, 1.64, 1.19),
    _gpu("NVIDIA L40S", "L40S", 48, 0.86, None, dcs=()),                                           # sold out: no availability at all
]


class FakeRunpod:
    """Just enough of https://api.runpod.io/v2."""

    def __init__(self, statuses=("PROVISIONING", "STARTING", "RUNNING"), gpus=None, keys=None):
        self.calls, self.pods, self.n = [], {}, 0
        self.statuses, self.gpus = list(statuses), gpus if gpus is not None else GPUS
        self.keys = list(keys if keys is not None else [PUB1 + " first", PUB2 + " second"])
        self.page_size, self.fail = 1000, {}

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        for (m, prefix), err in self.fail.items():
            if method == m and path.startswith(prefix):
                raise err
        if method == "GET" and path.startswith("/v2/catalog/gpus?"):
            q = dict(p.split("=") for p in path.split("?")[1].split("&"))
            assert q["include"] == "AVAILABILITY" and q["product"] == "POD"
            cloud = q["cloud"].lower()
            return {"gpus": [g for g in self.gpus if g[cloud]]}
        if method == "GET" and path == "/v2/account/ssh-keys":
            return {"keys": list(self.keys)}
        if method == "PUT" and path == "/v2/account/ssh-keys":
            self.keys = list(body["keys"])
            return {"keys": self.keys}
        if method == "POST" and path == "/v2/pods":
            self.n += 1
            pid = f"pod{self.n}"
            self.pods[pid] = {"id": pid, "name": body["name"], "env": body["env"], "polls": 0, "status": "PROVISIONING", "body": body}
            return {"id": pid, "status": "PROVISIONING", "name": body["name"]}
        if method == "GET" and path.startswith("/v2/pods?"):
            rows = [self._view(p, with_ssh=False) for p in self.pods.values()]
            q = dict(p.split("=") for p in path.split("?")[1].split("&"))
            start = int(q.get("cursor", "0"))
            page = rows[start:start + self.page_size]
            more = start + self.page_size < len(rows)
            return {"pods": page, "pagination": {"nextCursor": str(start + self.page_size) if more else None, "hasNextPage": more}}
        if method == "GET" and path.startswith("/v2/pods/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in self.pods:
                raise ApiError(404, json.dumps({"title": "Not Found", "status": 404, "detail": "pod not found"}))
            p = self.pods[pid]
            if p["status"] != "TERMINATED":
                p["status"] = self.statuses[min(p["polls"], len(self.statuses) - 1)]
                p["polls"] += 1
            return self._view(p)
        if method == "DELETE" and path.startswith("/v2/pods/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in self.pods:
                raise ApiError(404, "{}")
            self.pods[pid]["status"] = "TERMINATED"
            return {}                                                                       # 204: no body
        raise AssertionError(f"unexpected call {method} {path}")

    @staticmethod
    def _view(p, with_ssh=True):
        ssh = {"direct": {"host": "203.0.113.9", "port": 40022, "username": "root", "command": "ssh root@203.0.113.9 -p 40022"}} if (p["status"] == "RUNNING" and with_ssh) else {"direct": None}
        return {"id": p["id"], "name": p["name"], "status": p["status"], "env": p["env"], "ssh": ssh, "cost": 0.44}

    def creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/v2/pods"]

    def deleted(self):
        return [c[1].rsplit("/", 1)[1] for c in self.calls if c[0] == "DELETE"]


def prov(api=None):
    return RunpodProvider(http=api or FakeRunpod())


# ---- reading RunPod's names ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, mem, want", [
    ("H100 SXM", 80, "H100"), ("H100 NVL", 94, "H100"), ("RTX 4090", 24, "RTX4090"), ("RTX A6000", 48, "RTXA6000"), ("A100 PCIe", 80, "A100_80G"),
    ("A100 SXM", 40, "A100"), ("L40S", 48, "L40S"), ("Tesla V100", 16, "V100"), ("", 0, ""),
])
def test_the_gpu_family_comes_out_of_runpods_name(name, mem, want):
    assert family_of(name, mem) == want


def test_a_data_center_id_says_where_it_is():
    assert [runpod_area(d) for d in ("US-KS-2", "CA-MTL-1", "EU-RO-1", "EUR-IS-1", "AP-JP-1", "OC-AU-1", "XX-1", "")] == \
        ["north-america", "north-america", "europe", "europe", "asia-pacific", "asia-pacific", "", ""]


# ---- offers -------------------------------------------------------------------------------------------------------------

def test_offers_are_cheapest_first_one_row_per_data_center_and_always_containers():
    got = prov().offers(OfferQuery())
    assert got[0].instance_type == "NVIDIA GeForce RTX 4090" and (got[0].vendor, got[0].hourly_usd) == ("runpod-community", 0.31)
    assert {o.kind for o in got} == {"container"}                                      # the gpu-notebooks pack skips these: no Docker, no MIG
    assert [o.hourly_usd for o in got] == sorted(o.hourly_usd for o in got)
    h = next(o for o in got if o.gpu_type == "H100" and o.region == "EU-RO-1")
    assert (h.vendor, h.hourly_usd, h.vram_gb, h.num_gpus, h.area, h.region_name) == ("runpod-secure", 2.69, 80, 1, "europe", "EU Romania 1")
    assert not any(o.gpu_type == "L40S" for o in got)                                  # no data center has any: only with available_only=False
    assert not any(o.region == "AP-JP-1" for o in got)                                 # availability NONE


def test_price_is_per_gpu_times_the_count_and_a_count_above_the_maximum_is_left_out():
    four = prov().offers(OfferQuery(num_gpus=4, gpu_type="RTX4090"))
    assert [(o.vendor, o.hourly_usd, o.num_gpus) for o in four] == [("runpod-community", 1.24, 4), ("runpod-secure", 1.76, 4)]
    assert prov().offers(OfferQuery(num_gpus=8, gpu_type="RTX4090", vendor="runpod-community")) == []     # community maxCount is 4


def test_the_shape_other_clouds_use_works_here_too():
    p = prov()
    assert {o.instance_type for o in p.offers(OfferQuery(instance_type="H100"))} == {"NVIDIA H100 80GB HBM3"}
    assert {o.instance_type for o in p.offers(OfferQuery(instance_type="RTX4090x1"))} == {"NVIDIA GeForce RTX 4090"}
    assert [o.instance_type for o in p.offers(OfferQuery(instance_type="NVIDIA GeForce RTX 4090", vendor="runpod-secure"))] == ["NVIDIA GeForce RTX 4090"]
    eight = p.offers(OfferQuery(instance_type="H100x8"))
    assert [(o.num_gpus, o.hourly_usd) for o in eight] == [(8, 21.52), (8, 21.52)]      # H100x8 asks for eight GPUs, priced per GPU times eight
    assert p.offers(OfferQuery(instance_type="H100x16")) == []                           # above the maximum


def test_filters_by_vendor_region_area_price_and_kind():
    p = prov()
    assert {o.vendor for o in p.offers(OfferQuery(vendor="RunPod-Community"))} == {"runpod-community"}
    assert [o.region for o in p.offers(OfferQuery(gpu_type="H100", region="eu-ro-1"))] == ["EU-RO-1"]
    assert {o.region for o in p.offers(OfferQuery(gpu_type="H100", area="north-america"))} == {"US-KS-2"}
    assert {o.gpu_type for o in p.offers(OfferQuery(max_price=0.5))} == {"RTX4090"}
    assert p.offers(OfferQuery(kind="vm")) == [] and p.offers(OfferQuery(kind="baremetal")) == []
    assert p.offers(OfferQuery(kind="container"))


def test_sold_out_shows_when_asked_for_and_the_stock_flag_is_right():
    rows = prov().offers(OfferQuery(gpu_type="L40S", available_only=False))
    assert [(o.region, o.available) for o in rows] == [("", False)]
    assert [o.available for o in prov().offers(OfferQuery(gpu_type="H100", available_only=False, region="US-KS-2"))] == [True]      # LOW is still stock


def test_a_runpod_error_is_explained_without_the_key():
    api = FakeRunpod()
    api.fail[("GET", "/v2/catalog/gpus")] = ApiError(500, json.dumps({"title": "Boom", "status": 500, "detail": "try later", "errors": ["x"]}))
    with pytest.raises(ComputeError, match="could not list GPU types .*Boom try later x"):
        prov(api).offers(OfferQuery())
    api.fail[("GET", "/v2/catalog/gpus")] = ApiError(403, json.dumps({"detail": "this API key does not grant catalog:read", "status": 403, "title": "Forbidden"}))
    with pytest.raises(ComputeError, match=r"\(HTTP 403\): this API key does not grant catalog:read"):      # the real answer seen on 2026-11-19
        prov(api).offers(OfferQuery())
    for status, text in ((401, "rejected the API key"), (403, "rejected the API key"), (429, "too many requests")):
        api.fail[("GET", "/v2/catalog/gpus")] = ApiError(status, "{}")
        with pytest.raises(ComputeError, match=text) as e:
            prov(api).offers(OfferQuery())
        assert KEY not in str(e.value)


# ---- ssh keys -----------------------------------------------------------------------------------------------------------

def test_ssh_keys_are_plain_strings_so_the_fingerprint_is_the_id():
    keys = prov().ssh_keys()
    assert [(k.id, k.name) for k in keys][0] == (FP1, "test@labagent first") and keys[0].fingerprint == FP1


def test_adding_a_key_keeps_every_key_already_there_and_never_adds_twice():
    api = FakeRunpod()
    new = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEhnVm1yMExYOWhPYXdqRkpEbkVYMGZ1R3ZVbXc3 x"
    key, added = prov(api).add_ssh_key("labagent", new)
    assert added and key.name == "labagent" and key.id.startswith("SHA256:")
    puts = [c for c in api.calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][2]["keys"][:2] == [PUB1 + " first", PUB2 + " second"] and len(puts[0][2]["keys"]) == 3    # the replace carries the old keys
    again, added2 = prov(api).add_ssh_key("labagent", new)
    assert not added2 and again.id == key.id and len([c for c in api.calls if c[0] == "PUT"]) == 1


def test_a_failed_key_list_stops_before_any_replace_so_no_key_can_be_lost():
    api = FakeRunpod()
    api.fail[("GET", "/v2/account/ssh-keys")] = ApiError(500, "{}")
    with pytest.raises(ComputeError, match="could not list SSH keys"):
        prov(api).add_ssh_key("x", PUB1)
    assert not [c for c in api.calls if c[0] == "PUT"]


def test_a_bad_public_key_is_refused_before_any_call():
    api = FakeRunpod()
    with pytest.raises(ComputeError, match="not an OpenSSH public key"):
        prov(api).add_ssh_key("x", "not a key")
    assert api.calls == []


# ---- the pod ------------------------------------------------------------------------------------------------------------

def spec(**kw):
    return HostSpec(**{"name": "t-gpu", "instance_type": "H100", "vendor": "runpod-secure", "region": "EU-RO-1", "ssh_public_key": PUB1, "tags": ["labagent", "labagent-t"], **kw})


def test_create_sends_one_pod_with_the_key_in_the_environment_and_the_documented_fields():
    api = FakeRunpod()
    pid = prov(api).create(spec())
    (_, _, body), = api.creates()
    assert body == {"name": "t-gpu", "image": DEFAULT_IMAGE, "gpu": {"id": "NVIDIA H100 80GB HBM3", "count": 1}, "cloud": "SECURE", "ports": ["22/tcp"],
                    "env": {"LABAGENT_TAGS": "labagent,labagent-t", "PUBLIC_KEY": PUB1}, "startSsh": True, "disk": 50, "dataCenterIds": ["EU-RO-1"]}
    assert pid == "pod1"
    assert "PRIVATE" not in json.dumps(api.calls)


def test_create_without_an_inline_key_needs_the_key_to_be_in_the_account():
    api = FakeRunpod()
    prov(api).create(spec(ssh_public_key=None, ssh_key_id=FP1))
    assert "PUBLIC_KEY" not in api.creates()[0][2]["env"]                                # startSsh injects the account's keys
    with pytest.raises(ComputeError, match="not in your RunPod account"):
        prov(FakeRunpod()).create(spec(ssh_public_key=None, ssh_key_id="SHA256:nope"))


def test_create_refuses_before_renting_when_nothing_matches():
    api = FakeRunpod()
    with pytest.raises(ComputeError, match="no capacity on runpod-secure in AP-JP-1"):
        prov(api).create(spec(region="AP-JP-1"))
    with pytest.raises(ComputeError, match="runpod-secure or runpod-community"):
        prov(api).create(spec(vendor="lambda"))
    with pytest.raises(ComputeError, match="not an OpenSSH public key"):
        prov(api).create(spec(ssh_public_key="junk"))
    assert api.creates() == []


def test_a_create_rejected_by_runpod_says_why():
    api = FakeRunpod()
    api.fail[("POST", "/v2/pods")] = ApiError(422, json.dumps({"title": "Unprocessable", "status": 422, "detail": "bad image"}))
    with pytest.raises(ComputeError, match="could not create the pod .*Unprocessable bad image"):
        prov(api).create(spec())


def test_a_running_pod_is_active_only_once_its_ssh_port_is_assigned():
    api = FakeRunpod()
    p = prov(api)
    pid = p.create(spec())
    seen = [p.get(pid).status for _ in range(4)]
    assert seen == ["pending", "pending", "active", "active"]
    h = p.get(pid)
    assert (h.ip, h.ssh_port, h.ssh_user, h.tags) == ("203.0.113.9", 40022, "root", ["labagent", "labagent-t"])
    api.pods[pid]["status"] = "TERMINATED"
    assert p.get(pid).status == "deleted"


def test_find_adopts_only_a_live_pod_with_this_name_and_tag_across_pages():
    api = FakeRunpod()
    api.page_size = 1
    p = prov(api)
    other = p.create(spec(name="other", tags=["labagent", "labagent-o"]))
    mine = p.create(spec())
    assert p.find("t-gpu", "labagent-t").id == mine
    assert p.find("t-gpu", "labagent-o") is None and p.find("zzz", "labagent-t") is None
    p.delete(mine)
    assert p.find("t-gpu", "labagent-t") is None and p.find("other", "labagent-o").id == other


def test_delete_terminates_the_pod_and_a_second_delete_is_quiet():
    api = FakeRunpod()
    p = prov(api)
    pid = p.create(spec())
    p.delete(pid)
    p.delete(pid)
    p.delete("never-existed")
    assert api.deleted()[:1] == [pid] and api.pods[pid]["status"] == "TERMINATED"
    api.fail[("DELETE", "/v2/pods/pod1")] = ApiError(409, json.dumps({"title": "Conflict", "detail": "cluster pod"}))
    with pytest.raises(ComputeError, match="could not terminate the pod .*Conflict cluster pod"):
        p.delete(pid)


def test_validate_names_the_options_when_it_refuses():
    p = prov()
    assert p.validate(spec())["hourly_cents"] == 269
    with pytest.raises(ComputeError, match="set vendor"):
        p.validate(spec(vendor=None))
    with pytest.raises(ComputeError, match="not available on runpod-secure in AP-JP-1.*Available H100"):
        p.validate(spec(region="AP-JP-1"))
    with pytest.raises(ComputeError, match="1 to 50 letters"):
        p.validate(spec(name="bad name"))


# ---- credentials and registration ---------------------------------------------------------------------------------------

def test_the_key_is_found_in_the_environment_the_credentials_file_or_the_key_file(home, monkeypatch, tmp_path):   # noqa: F811
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    with pytest.raises(ComputeError, match="no RunPod API key: set RUNPOD_API_KEY"):
        RunpodProvider()
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)
    assert find_secret("runpod", "RUNPOD_API_KEY", key_file="runpod_api_key")[:2] == (KEY, "environment")
    monkeypatch.delenv("RUNPOD_API_KEY")
    keys = tmp_path / "home" / "keys"
    keys.mkdir(parents=True)
    (keys / "runpod_api_key").write_text(KEY + "\n")
    (keys / "runpod_api_key").chmod(0o600)
    assert find_secret("runpod", "RUNPOD_API_KEY", key_file="runpod_api_key")[:2] == (KEY, "key file")


def test_runpod_is_listed_and_does_not_enter_the_gpu_notebooks_platforms(home, monkeypatch):    # noqa: F811
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    row = {c["id"]: c for c in catalog()}["runpod"]
    assert row["capabilities"] == ["offers", "ssh_keys", "launch"] and row["credentials"]["configured"] is False
    assert row["credentials"]["env_var"] == "RUNPOD_API_KEY" and row["credentials"]["key_file"].endswith("runpod_api_key")
    from labagent.packs.gpu_notebooks.pack import GpuNotebooks
    assert "runpod" in providers.PROVIDERS and "runpod" not in GpuNotebooks.platforms and RunpodProvider.auto_delete is False


# ---- what the offers command tells a person to run next ---------------------------------------------------------------

def test_a_container_offer_points_at_the_container_pack_and_shows_no_mig_estimate():
    from labagent.compute.cli import offers_text
    got = prov().offers(OfferQuery(gpu_type="H100", region="EU-RO-1"))
    text = offers_text("runpod", got, len(got), False)
    assert not re.search(r"container\s+7\b", text)                                      # no MIG estimate in the LABS column
    assert "labagent launch gpu-notebooks-container --name <name> --param cloud=runpod" in text and "labs" not in text.split("Next")[1]
    assert "launch gpu-notebooks --name" not in text and "ssh_key_id=<id>" not in text
