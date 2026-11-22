"""Lambda Cloud as a compute provider, against a fake Lambda API (no network, no key, no cost).

The fake answers with the response shapes the real API gave to read-only calls on 2026-11-14: `{"data": ...}`, instance types keyed by
name with `regions_with_capacity_available`, `page_token` for the instance list, and error bodies with `error.message`.
"""
import base64
import json

import pytest

from labagent import cli
from labagent.compute import providers
from labagent.compute.errors import ComputeError
from labagent.compute.lambda_cloud import LambdaProvider, TAG_MAX, gpu_of, lambda_area
from labagent.compute.providers import ApiError, HostSpec, OfferQuery, catalog, find_secret, read_secret
from labagent.packs.base import load_state, state_path
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, host_name, host_tag
from tests.test_gpu_notebooks import FP1, FP2, FakeSsh, PUB1, PUB2, home, run  # noqa: F401  (home is a fixture)
from tests.test_gpu_ssh_keys import ticks

LAMBDA_KEY = "lambda-test-key-0123456789"


def _type(name, gpu, price, gpus, vcpus=26, mem=200, disk=1024, arch=None):
    t = {"name": name, "description": f"{gpus}x {gpu}", "gpu_description": gpu, "price_cents_per_hour": price,
         "specs": {"vcpus": vcpus, "memory_gib": mem, "storage_gib": disk, "gpus": gpus}}
    if arch:
        t["architecture"] = arch
    return t


def _region(name, description="x"):
    return {"name": name, "description": description}


TYPES = {
    "gpu_1x_h100_pcie": {"instance_type": _type("gpu_1x_h100_pcie", "H100 (80 GB PCIe)", 329, 1), "regions_with_capacity_available": [_region("us-west-3", "California, USA")]},
    "gpu_1x_h100_sxm5": {"instance_type": _type("gpu_1x_h100_sxm5", "H100 (80 GB SXM5)", 429, 1, mem=225), "regions_with_capacity_available": [_region("us-southeast-1")]},
    "gpu_8x_h100_sxm5": {"instance_type": _type("gpu_8x_h100_sxm5", "H100 (80 GB SXM5)", 3192, 8, 208, 1800), "regions_with_capacity_available": []},      # sold out
    "gpu_1x_a10": {"instance_type": _type("gpu_1x_a10", "A10 (24 GB PCIe)", 129, 1), "regions_with_capacity_available": [_region("us-east-1"), _region("us-west-1")]},
    "gpu_8x_a100": {"instance_type": _type("gpu_8x_a100", "A100 (40 GB SXM4)", 1592, 8, 124, 1800),
                    "regions_with_capacity_available": [_region("europe-central-1", "Germany"), _region("me-west-1", "Israel"), _region("asia-northeast-2", "Japan")]},
    "gpu_8x_a100_80gb_sxm4": {"instance_type": _type("gpu_8x_a100_80gb_sxm4", "A100 (80 GB SXM4)", 2232, 8, 240, 1800), "regions_with_capacity_available": []},
    "gpu_1x_gh200": {"instance_type": _type("gpu_1x_gh200", "GH200 (96 GB)", 199, 1, arch="arm64"), "regions_with_capacity_available": [_region("us-east-3")]},   # ARM: left out
    "cpu_4x_general": {"instance_type": _type("cpu_4x_general", "", 10, 0), "regions_with_capacity_available": [_region("us-east-1")]},                       # no GPU: left out
}


class FakeLambda:
    """Just enough of https://cloud.lambda.com. `statuses` is what a new instance reports on each poll."""

    def __init__(self, statuses=("booting", "booting", "active"), types=None):
        self.calls, self.instances, self.rulesets = [], {}, []
        self.statuses, self.types, self.n = list(statuses), types if types is not None else TYPES, 0
        self.keys = [{"id": "k1", "name": "labagent-test", "public_key": PUB1}, {"id": "k2", "name": "other", "public_key": PUB2}]
        self.page_size = 100
        self.fail = {}                              # (method, path-prefix) -> ApiError to raise

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        for (m, prefix), err in self.fail.items():
            if method == m and path.startswith(prefix):
                raise err
        if method == "GET" and path == "/api/v1/instance-types":
            return {"data": self.types}
        if method == "GET" and path == "/api/v1/ssh-keys":
            return {"data": self.keys}
        if method == "POST" and path == "/api/v1/ssh-keys":
            self.keys.append({"id": f"k{len(self.keys) + 1}", "name": body["name"], "public_key": body["public_key"]})
            return {"data": {"id": self.keys[-1]["id"], "name": body["name"], "public_key": body["public_key"]}}
        if method == "GET" and path == "/api/v1/firewall-rulesets":
            return {"data": self.rulesets}
        if method == "POST" and path == "/api/v1/firewall-rulesets":
            self.rulesets.append({"id": f"fw{len(self.rulesets) + 1}", "name": body["name"], "region": {"name": body["region"], "description": ""}, "rules": body["rules"]})
            return {"data": self.rulesets[-1]}
        if method == "POST" and path == "/api/v1/instance-operations/launch":
            self.n += 1
            iid = f"i{self.n}"
            self.instances[iid] = {"id": iid, "name": body["name"], "tags": body.get("tags", []), "polls": 0, "status": "booting", "body": body}
            return {"data": {"instance_ids": [iid]}}
        if method == "GET" and path.startswith("/api/v1/instances?"):
            rows = [self._view(i) for i in self.instances.values()]
            query = dict(p.split("=") for p in path.split("?")[1].split("&"))
            start = int(query.get("page_token", "0"))
            page = rows[start:start + self.page_size]
            more = start + self.page_size < len(rows)
            return {"data": page, "page_token": str(start + self.page_size) if more else None}
        if method == "GET" and path.startswith("/api/v1/instances/"):
            iid = path.rsplit("/", 1)[1]
            if iid not in self.instances:
                raise ApiError(404, json.dumps({"error": {"code": "global/object-does-not-exist", "message": "Instance does not exist"}}))
            i = self.instances[iid]
            if i["status"] != "terminating":
                i["status"] = self.statuses[min(i["polls"], len(self.statuses) - 1)]
                i["polls"] += 1
            return {"data": self._view(i)}
        if method == "POST" and path == "/api/v1/instance-operations/terminate":
            gone = []
            for iid in body["instance_ids"]:
                if iid not in self.instances:
                    raise ApiError(404, json.dumps({"error": {"code": "global/object-does-not-exist", "message": f"Instance {iid} not found"}}))
                self.instances[iid]["status"] = "terminating"
                gone.append(self._view(self.instances[iid]))
            return {"data": {"terminated_instances": gone}}
        raise AssertionError(f"unexpected call {method} {path}")

    @staticmethod
    def _view(i):
        active = i["status"] == "active"
        return {"id": i["id"], "name": i["name"], "status": i["status"], "ip": "203.0.113.9" if active else None, "tags": i["tags"],
                "jupyter_token": "never-read-this", "jupyter_url": "https://jupyter.example/?token=never-read-this"}

    def launches(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/api/v1/instance-operations/launch"]

    def terminated(self):
        return [c[2]["instance_ids"][0] for c in self.calls if c[1] == "/api/v1/instance-operations/terminate"]


def prov(api=None):
    return LambdaProvider(http=api or FakeLambda())


def pack_params(**kw):
    return {"cloud": "lambda", "instance_type": "H100", "region": "us-west-3", "ssh_key_id": "k1", **kw}


def lrun(action, api, name="t", params=None, log=None, **extra):
    extra.setdefault("provider", lambda pid: LambdaProvider(http=api))
    return run(action, api, name=name, params=params if params is not None else pack_params(), log=log, **extra)


# ---- reading what Lambda describes --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("H100 (80 GB PCIe)", ("H100", 80, "PCIe")),
    ("H100 (80 GB SXM5)", ("H100", 80, "SXM5")),
    ("A100 (40 GB SXM4)", ("A100", 40, "SXM4")),
    ("A100 (80 GB SXM4)", ("A100_80G", 80, "SXM4")),                 # the name Shadeform and the rest of labagent use
    ("RTX A6000 (48 GB)", ("RTXA6000", 48, "")),
    ("Tesla V100 (16 GB SXM2)", ("V100", 16, "SXM2")),
    ("B200 (180 GB SXM6)", ("B200", 180, "SXM6")),
    ("", ("", 0, "")),
    ("something else entirely", ("", 0, "")),
])
def test_the_gpu_family_and_memory_come_out_of_lambdas_description(text, want):
    assert gpu_of(text) == want


def test_a_region_code_says_where_it_is():
    assert [lambda_area(r) for r in ("us-west-3", "europe-central-1", "asia-northeast-2", "australia-east-1", "me-west-1", "")] == \
        ["north-america", "europe", "asia-pacific", "asia-pacific", "", ""]


# ---- offers -------------------------------------------------------------------------------------------------------------

def test_offers_are_cheapest_first_with_one_row_per_region_and_leave_out_what_the_lab_cannot_use():
    got = prov().offers(OfferQuery())
    assert [(o.instance_type, o.region) for o in got][:4] == [("gpu_1x_a10", "us-east-1"), ("gpu_1x_a10", "us-west-1"), ("gpu_1x_h100_pcie", "us-west-3"),
                                                            ("gpu_1x_h100_sxm5", "us-southeast-1")]
    names = {o.instance_type for o in got}
    assert "gpu_1x_gh200" not in names and "cpu_4x_general" not in names                      # ARM, and no GPU
    assert "gpu_8x_h100_sxm5" not in names                                                    # sold out: only with available_only=False
    h = next(o for o in got if o.instance_type == "gpu_1x_h100_pcie")
    assert (h.vendor, h.gpu_type, h.vram_gb, h.num_gpus, h.hourly_usd, h.interconnect, h.kind, h.area) == ("lambda", "H100", 80, 1, 3.29, "PCIe", "vm", "north-america")
    assert (h.vcpus, h.memory_gb, h.storage_gb, h.region_name) == (26, 200, 1024, "California, USA")


def test_the_shape_other_clouds_use_works_here_too():
    one = [o.instance_type for o in prov().offers(OfferQuery(instance_type="H100"))]
    assert one == ["gpu_1x_h100_pcie", "gpu_1x_h100_sxm5"]                                    # H100 means one GPU
    assert [o.instance_type for o in prov().offers(OfferQuery(instance_type="H100x8", available_only=False))] == ["gpu_8x_h100_sxm5"]
    assert [o.instance_type for o in prov().offers(OfferQuery(instance_type="gpu_1x_h100_sxm5"))] == ["gpu_1x_h100_sxm5"]       # Lambda's own name
    assert [o.instance_type for o in prov().offers(OfferQuery(instance_type="A100x8"))] == ["gpu_8x_a100"] * 3
    assert prov().offers(OfferQuery(instance_type="H100x2")) == []


def test_filters_by_gpu_count_region_price_and_area():
    p = prov()
    assert {o.instance_type for o in p.offers(OfferQuery(gpu_type="a100"))} == {"gpu_8x_a100"}
    assert [o.region for o in p.offers(OfferQuery(region="US-WEST-1"))] == ["us-west-1"]
    assert {o.instance_type for o in p.offers(OfferQuery(max_price=1.5))} == {"gpu_1x_a10"}
    assert [o.region for o in p.offers(OfferQuery(instance_type="A100x8", area="europe"))] == ["europe-central-1"]
    assert [o.region for o in p.offers(OfferQuery(instance_type="A100x8", area="asia-pacific"))] == ["asia-northeast-2"]
    assert len(p.offers(OfferQuery(instance_type="A100x8", area="any"))) == 3                  # Israel has no area but is found by "any"
    assert p.offers(OfferQuery(vendor="massedcompute")) == [] and p.offers(OfferQuery(kind="baremetal")) == []
    assert [o.num_gpus for o in p.offers(OfferQuery(num_gpus=8))] == [8, 8, 8]


def test_sold_out_shapes_show_with_no_region_when_asked_for():
    rows = prov().offers(OfferQuery(instance_type="H100x8", available_only=False))
    assert [(o.region, o.available) for o in rows] == [("", False)]


def test_a_lambda_error_is_explained_without_the_key():
    api = FakeLambda()
    api.fail[("GET", "/api/v1/instance-types")] = ApiError(500, json.dumps({"error": {"message": "boom", "suggestion": "try later"}}))
    with pytest.raises(ComputeError, match="could not list instance types .*boom try later"):
        prov(api).offers(OfferQuery())
    api.fail[("GET", "/api/v1/instance-types")] = ApiError(401, "{}")
    with pytest.raises(ComputeError, match="rejected the API key"):
        prov(api).offers(OfferQuery())


# ---- ssh keys -----------------------------------------------------------------------------------------------------------

def test_ssh_keys_show_a_fingerprint_and_nothing_secret():
    api = FakeLambda()
    api.keys[0]["private_key"] = "SECRET-PRIVATE-HALF"
    keys = prov(api).ssh_keys()
    assert [(k.id, k.name, k.fingerprint, k.is_default) for k in keys] == [("k1", "labagent-test", FP1, False), ("k2", "other", FP2, False)]
    assert "SECRET-PRIVATE-HALF" not in repr(keys) and PUB1.split()[1] not in repr(keys)


def test_adding_a_key_that_is_already_there_adds_nothing():
    api = FakeLambda()
    key, added = prov(api).add_ssh_key("whatever", PUB1)
    assert (key.id, added) == ("k1", False) and not [c for c in api.calls if c[0] == "POST"]


def test_a_new_key_sends_only_the_name_and_the_public_half():
    api = FakeLambda()
    pub = "ssh-ed25519 " + base64.b64encode(b"labagent-test-key-three").decode() + " me@laptop"
    key, added = prov(api).add_ssh_key("fresh", pub + "\n")
    assert added and key.id == "k3" and key.fingerprint == providers.fingerprint_of(pub)
    (_, _, body), = [c for c in api.calls if c[0] == "POST"]
    assert body == {"name": "fresh", "public_key": pub}                                      # no generating of a pair: Lambda would send us the private half


def test_something_that_is_not_a_public_key_is_refused_before_any_call():
    api = FakeLambda()
    with pytest.raises(ComputeError, match="not an OpenSSH public key"):
        prov(api).add_ssh_key("x", "not a key at all")
    assert api.calls == []


# ---- validate -----------------------------------------------------------------------------------------------------------

def test_validate_gives_the_price_and_names_what_is_available_when_it_is_not():
    p = prov()
    spec = HostSpec(name="x", instance_type="H100", region="us-west-3")
    info = p.validate(spec)
    assert (info["hourly_cents"], info["gpu_type"], info["instance_type"]) == (329, "H100", "gpu_1x_h100_pcie")
    with pytest.raises(ComputeError) as e:
        p.validate(HostSpec(name="x", instance_type="H100", region="nowhere-1"))
    assert "not available in nowhere-1" in str(e.value) and "region=us-west-3 gpu_1x_h100_pcie $3.29/h" in str(e.value)
    with pytest.raises(ComputeError, match="set region"):
        p.validate(HostSpec(name="x", instance_type="H100"))
    with pytest.raises(ComputeError, match="image 'windows' is not offered"):
        p.validate(HostSpec(name="x", instance_type="H100", region="us-west-3", os_image="windows"))
    with pytest.raises(ComputeError, match="1 to 50"):
        p.validate(HostSpec(name="bad name!", instance_type="H100", region="us-west-3"))


# ---- creating, finding, reading and deleting ----------------------------------------------------------------------------

def test_create_asks_for_the_documented_launch_and_opens_only_the_web_ports():
    api = FakeLambda()
    hid = prov(api).create(HostSpec(name="labagent-demo", instance_type="H100", region="us-west-3", ssh_key_id="k1", tags=["labagent", host_tag("demo")]))
    assert hid == "i1"
    (_, _, body), = api.launches()
    assert body["region_name"] == "us-west-3" and body["instance_type_name"] == "gpu_1x_h100_pcie" and body["name"] == "labagent-demo"
    assert body["ssh_key_names"] == ["labagent-test"]                                        # by NAME: the pack's id is looked up
    assert body["image"] == {"family": "lambda-stack-24-04"}
    assert body["tags"] == [{"key": "labagent-0", "value": "labagent"}, {"key": "labagent-1", "value": host_tag("demo")}]
    assert body["firewall_rulesets"] == [{"id": "fw1"}]
    assert [(r["protocol"], r["port_range"], r["source_network"]) for r in api.rulesets[0]["rules"]] == [("tcp", [80, 80], "0.0.0.0/0"), ("tcp", [443, 443], "0.0.0.0/0")]
    assert api.rulesets[0]["name"] == "labagent-web-us-west-3" and api.rulesets[0]["region"]["name"] == "us-west-3"
    assert not [c for c in api.calls if "global" in c[1]]                                     # the account-wide rules are not even read
    assert not [c for c in api.calls if c[0] in ("PATCH", "PUT", "DELETE")]                   # the account's own rules are never edited or removed


def test_the_firewall_ruleset_is_made_once_per_region_and_reused():
    api = FakeLambda()
    p = prov(api)
    spec = dict(instance_type="H100", region="us-west-3", ssh_key_id="k1")
    p.create(HostSpec(name="a", **spec))
    p.create(HostSpec(name="b", **spec))
    assert len(api.rulesets) == 1 and [c[0] for c in api.launches()] == ["POST", "POST"]
    assert api.launches()[1][2]["firewall_rulesets"] == [{"id": "fw1"}]
    p.create(HostSpec(name="c", instance_type="A10", region="us-east-1", ssh_key_id="k1"))
    assert [r["name"] for r in api.rulesets] == ["labagent-web-us-west-3", "labagent-web-us-east-1"]       # rulesets are per region


def test_a_long_tag_is_cut_to_lambdas_limit_and_still_found_again():
    api = FakeLambda()
    long_tag = "labagent:gpu-notebooks:" + "n" * 150
    p = prov(api)
    p.create(HostSpec(name="labagent-n", instance_type="H100", region="us-west-3", ssh_key_id="k1", tags=["labagent", long_tag]))
    assert all(len(t["value"]) <= TAG_MAX for t in api.launches()[0][2]["tags"])
    assert p.find("labagent-n", long_tag).id == "i1"


@pytest.mark.parametrize("spec, why", [
    (dict(ssh_key_id="nope", region="us-west-3"), "SSH key nope is not in your Lambda account"),
    (dict(ssh_key_id=None, region="us-west-3"), r"SSH key \(none\) is not in your Lambda account"),
    (dict(ssh_key_id="k1", region=None), "set region"),
    (dict(ssh_key_id="k1", region="nowhere-1"), "has no capacity in nowhere-1"),
])
def test_a_launch_that_cannot_work_is_refused_before_anything_is_rented(spec, why):
    api = FakeLambda()
    with pytest.raises(ComputeError, match=why):
        prov(api).create(HostSpec(name="x", instance_type="H100", **spec))
    assert api.launches() == [] and api.rulesets == []


def test_if_the_firewall_cannot_be_set_up_nothing_is_rented():
    api = FakeLambda()
    api.fail[("POST", "/api/v1/firewall-rulesets")] = ApiError(400, json.dumps({"error": {"message": "invalid rule"}}))
    with pytest.raises(ComputeError, match="firewall rules .* invalid rule"):
        prov(api).create(HostSpec(name="x", instance_type="H100", region="us-west-3", ssh_key_id="k1"))
    assert api.launches() == []


def test_a_launch_lambda_refuses_says_why():
    api = FakeLambda()
    api.fail[("POST", "/api/v1/instance-operations/launch")] = ApiError(400, json.dumps({"error": {"code": "instance-operations/launch/insufficient-capacity",
                                                                                                    "message": "Not enough capacity", "suggestion": "Try another region"}}))
    with pytest.raises(ComputeError, match="launch the instance .*Not enough capacity Try another region"):
        prov(api).create(HostSpec(name="x", instance_type="H100", region="us-west-3", ssh_key_id="k1"))


def test_a_launch_that_answers_with_no_id_is_reported_as_a_possible_stray_vm():
    api = FakeLambda()

    def no_ids(method, path, body=None):
        if method == "POST" and path == "/api/v1/instance-operations/launch":
            return {"data": {"instance_ids": []}}
        return api(method, path, body)
    with pytest.raises(ComputeError, match="returned no instance id"):
        LambdaProvider(http=no_ids).create(HostSpec(name="x", instance_type="H100", region="us-west-3", ssh_key_id="k1"))


def test_find_needs_the_exact_name_and_tag_follows_pages_and_skips_the_dead():
    api = FakeLambda()
    api.page_size = 2
    p = prov(api)
    tag = host_tag("t")
    for i in range(5):                                                                        # four unrelated VMs, then mine on the last page
        api("POST", "/api/v1/instance-operations/launch", {"name": host_name("t") if i == 4 else f"other-{i}", "tags": [{"key": "labagent-1", "value": tag if i in (3, 4) else "x"}]})
    assert [c[1] for c in api.calls if c[1].startswith("/api/v1/instances?")] == []
    assert p.find(host_name("t"), tag).id == "i5"
    assert len([c for c in api.calls if c[1].startswith("/api/v1/instances?")]) == 3          # it walked 3 pages of 2
    assert p.find(host_name("t"), "labagent:gpu-notebooks:other") is None                      # same name, other tag: not mine
    api.instances["i5"]["status"] = "terminated"
    assert p.find(host_name("t"), tag) is None


def test_get_maps_statuses_and_a_missing_instance_is_none():
    api = FakeLambda(statuses=("booting", "unhealthy", "active", "preempted"))
    p = prov(api)
    p.create(HostSpec(name="x", instance_type="H100", region="us-west-3", ssh_key_id="k1"))
    seen = [p.get("i1") for _ in range(4)]
    assert [h.status for h in seen] == ["pending", "unhealthy", "active", "deleted"]
    assert seen[2].ip == "203.0.113.9" and seen[2].ssh_user == "ubuntu" and seen[2].ssh_port == 22 and seen[0].ip is None
    assert p.get("nope") is None
    assert "never-read-this" not in repr(seen)                                                # the instance's own Jupyter token is not read


def test_delete_terminates_and_an_already_gone_instance_is_fine():
    api = FakeLambda()
    p = prov(api)
    p.create(HostSpec(name="x", instance_type="H100", region="us-west-3", ssh_key_id="k1"))
    p.delete("i1")
    assert api.terminated() == ["i1"] and api.instances["i1"]["status"] == "terminating"
    p.delete("i9")                                                                             # 404: nothing to do
    api.fail[("POST", "/api/v1/instance-operations/terminate")] = ApiError(500, "oops")
    with pytest.raises(ComputeError, match="could not terminate the instance"):
        p.delete("i1")


# ---- where the key comes from --------------------------------------------------------------------------------------------

@pytest.fixture
def lhome(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.delenv("LAMBDA_API_KEY", raising=False)
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    (tmp_path / "keys").mkdir()
    return tmp_path


def test_the_key_comes_from_the_environment_then_the_credentials_file_then_the_key_file(lhome, monkeypatch):
    kf = lhome / "keys" / "lambda_api_key"
    kf.write_text("from-key-file\n")
    kf.chmod(0o600)
    assert find_secret("lambda", "LAMBDA_API_KEY", key_file="lambda_api_key") == ("from-key-file", "key file", [])
    (lhome / "credentials").write_text("[lambda]\nkey = from-credentials\n")
    (lhome / "credentials").chmod(0o600)
    assert find_secret("lambda", "LAMBDA_API_KEY", key_file="lambda_api_key")[:2] == ("from-credentials", "credentials file")
    monkeypatch.setenv("LAMBDA_API_KEY", "from-env")
    assert find_secret("lambda", "LAMBDA_API_KEY", key_file="lambda_api_key")[:2] == ("from-env", "environment")
    assert read_secret("lambda", "LAMBDA_API_KEY", key_file="lambda_api_key") == ("from-env", [])


def test_a_key_file_other_users_can_read_warns_and_still_works(lhome):
    kf = lhome / "keys" / "lambda_api_key"
    kf.write_text(LAMBDA_KEY + "\n")
    kf.chmod(0o644)
    value, warnings = read_secret("lambda", "LAMBDA_API_KEY", key_file="lambda_api_key")
    assert value == LAMBDA_KEY and len(warnings) == 1 and "chmod 600" in warnings[0] and LAMBDA_KEY not in warnings[0]
    assert "chmod 600" in LambdaProvider().warnings[0]                                        # the provider carries the warning to the command line


def test_a_key_file_that_is_not_one_line_is_not_taken_for_a_key(lhome):
    (lhome / "keys" / "lambda_api_key").write_text("line one\nline two\n")
    assert read_secret("lambda", "LAMBDA_API_KEY", key_file="lambda_api_key")[0] is None
    with pytest.raises(ComputeError, match="no Lambda API key: set LAMBDA_API_KEY"):
        LambdaProvider()


def test_the_cloud_list_shows_lambda_and_where_to_put_its_key_but_never_the_key(lhome, monkeypatch, capsys):
    (lhome / "keys" / "lambda_api_key").write_text(LAMBDA_KEY)
    (lhome / "keys" / "lambda_api_key").chmod(0o600)
    assert cli.main(["compute", "clouds", "--json"]) == 0
    raw = capsys.readouterr().out
    lam = next(c for c in json.loads(raw)["clouds"] if c["id"] == "lambda")
    assert lam["title"] == "Lambda" and lam["capabilities"] == ["offers", "ssh_keys", "launch"]
    assert lam["credentials"]["configured"] is True and lam["credentials"]["source"] == "key file" and lam["credentials"]["env_var"] == "LAMBDA_API_KEY"
    assert lam["credentials"]["key_file"].endswith("keys/lambda_api_key") and LAMBDA_KEY not in raw
    (lhome / "keys" / "lambda_api_key").unlink()
    assert cli.main(["compute", "clouds"]) == 0
    assert "missing: set LAMBDA_API_KEY or [lambda] in" in capsys.readouterr().out
    assert [c["id"] for c in catalog()] == ["lambda", "nebius", "nebius-devlab", "runpod", "shadeform"]


# ---- the pack on Lambda -------------------------------------------------------------------------------------------------

def test_a_launch_on_lambda_logs_in_as_ubuntu_and_says_the_vm_is_not_deleted_for_you(home):
    api, ssh, lines = FakeLambda(), FakeSsh(), []
    env = lrun("launch", api, ssh=ssh, log=lines.append, params=pack_params(ssh_key_file="/keys/mine"))
    assert env.status == "ok", env.to_dict()
    out = env.summary["outputs"]
    assert (out["cloud"], out["host_id"], out["ip"], out["ssh_user"], out["hourly_cents"], out["auto_delete"]) == ("lambda", "i1", "203.0.113.9", "ubuntu", 329, False)
    assert (out["ssh_key_id"], out["ssh_key_name"], out["ssh_key_fingerprint"], out["ssh_key_verified"]) == ("k1", "labagent-test", FP1, True)
    assert ssh.args[:3] == ("203.0.113.9", "ubuntu", 22)
    text = "\n".join(lines)
    assert "NOT deleted by lambda" in text and "bills until you run `labagent destroy gpu-notebooks --name t`" in text
    assert "(lambda), deleted automatically" not in text and "NOT deleted automatically: it bills until you run: labagent destroy gpu-notebooks --name t" in text
    assert api.instances["i1"]["body"]["firewall_rulesets"] == [{"id": "fw1"}] and api.terminated() == []


def test_location_picks_the_cheapest_region_in_that_area_on_lambda(home):
    api = FakeLambda()
    p = {"cloud": "lambda", "instance_type": "A100x8", "location": "europe", "ssh_key_id": "k1", "mode": "mig"}
    env = lrun("launch", api, params=p)
    assert env.status == "ok", env.to_dict()
    assert api.launches()[0][2]["region_name"] == "europe-central-1" and env.summary["outputs"]["region"] == "europe-central-1"
    assert load_state("gpu-notebooks", "t")["vendor"] == "lambda"


def test_a_gpu_without_mig_is_refused_before_anything_is_rented(home):
    api = FakeLambda()
    env = lrun("launch", api, params=pack_params(instance_type="gpu_1x_a10", region="us-east-1"))
    assert env.status == "failed" and "A10 has no MIG" in env.error and "mode=shared" in env.error
    assert api.launches() == [] and api.rulesets == []


def test_a_launch_that_fails_terminates_the_lambda_vm_since_lambda_will_not(home):
    api = FakeLambda()
    env = lrun("launch", api, ssh=FakeSsh(down_for=10 ** 6), clock=ticks(), params=pack_params(ssh_key_file="/keys/mine"))
    assert env.status == "failed" and "cannot log in to the VM over ssh" in env.error
    assert api.terminated() == ["i1"] and env.summary["cleanup"] == {"action": "deleted", "host_id": "i1"}
    assert not state_path("gpu-notebooks", "t").exists()


def test_a_vm_lambda_reports_as_gone_while_starting_is_a_plain_failure(home):
    env = lrun("launch", FakeLambda(statuses=("booting", "terminated")))
    assert env.status == "failed" and "the VM was deleted while starting (the cloud failed it)" in env.error      # no mention of `hours`: Lambda has no such limit


def test_a_rerun_after_success_rents_nothing_more_and_destroy_terminates_only_this_one(home):
    api = FakeLambda()
    lrun("launch", api, name="a")
    lrun("launch", api, name="b")
    assert lrun("launch", api, name="a").summary["skipped"] >= 1 and len(api.launches()) == 2
    assert lrun("destroy", api, name="a").status == "ok"
    assert api.terminated() == ["i1"] and api.instances["i2"]["status"] != "terminating"
    assert lrun("destroy", api, name="a").status == "ok"                                      # nothing left: still fine


def test_destroy_after_a_lost_state_file_finds_the_lambda_vm_by_name_and_tag(home):
    api = FakeLambda()
    api("POST", "/api/v1/instance-operations/launch", {"name": host_name("t"), "tags": [{"key": "labagent-1", "value": host_tag("t")}]})
    assert lrun("destroy", api).status == "ok" and api.terminated() == ["i1"]


def test_the_lambda_key_never_reaches_state_envelope_logs_or_errors(home, monkeypatch):
    from labagent.compute import lambda_cloud
    seen = []

    def spying_http(key):
        inner = FakeLambda()

        def call(m, p, b=None):
            seen.append(json.dumps([m, p, b]))
            return inner(m, p, b)
        return call
    monkeypatch.setenv("LAMBDA_API_KEY", LAMBDA_KEY)
    monkeypatch.setattr(lambda_cloud, "make_http", spying_http)
    ssh, lines = FakeSsh(), []
    env = run("launch", None, name="t", params=pack_params(), log=lines.append, ssh=ssh, provider=providers.make)
    assert env.status == "ok", env.to_dict()
    blob = json.dumps(env.to_dict()) + json.dumps(load_state("gpu-notebooks", "t")) + "".join(seen) + "".join(ssh.commands()) + "".join(lines)
    assert LAMBDA_KEY not in blob


def test_the_pack_offers_lambda_as_a_platform_and_names_its_region_format():
    pack = GpuNotebooks()
    assert pack.platforms == ("shadeform", "lambda", "nebius")
    params = {p.name: p for p in pack.params}
    assert "lambda" in params["cloud"].help and "us-west-3" in params["region"].help and "no such setting" in params["hours"].help
