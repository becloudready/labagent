"""`labagent offers` and `labagent ssh-keys`: parsing, filtering, output shape and exit codes, against a fake API."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from labagent import cli
from labagent.packs.base import PackError
from labagent.packs.gpu_notebooks import offers as offers_cli
from labagent.packs.gpu_notebooks import providers
from labagent.packs.gpu_notebooks.providers import ApiError, OfferQuery, ShadeformProvider

OS_A = "ubuntu24.04_cuda12.4_shade_os"
OS_B = "ubuntu22.04_cuda12.2_shade_os"


def shape(cloud, itype, gpu, n, cents, regions, kind="vm", oses=(OS_A,), vram=80):
    return {"cloud": cloud, "shade_instance_type": itype, "hourly_price": cents, "deployment_type": kind, "gpu_type": gpu, "num_gpus": n,
            "vcpus": 16, "memory_in_gb": 192, "storage_in_gb": 1000, "interconnect": "pcie", "boot_time": {"min_boot_in_sec": 60, "max_boot_in_sec": 600},
            "configuration": {"os_options": list(oses), "vram_per_gpu_in_gb": vram},
            "availability": [{"region": r, "available": a, "display_name": r.upper(), "rental_type": rt} for r, a, rt in regions]}


TYPES = {"instance_types": [
    shape("beta", "H100", "H100", 1, 330, [("eu-1", True, "on_demand"), ("eu-2", False, "on_demand")], oses=(OS_A, OS_B)),
    shape("alpha", "H100", "H100", 1, 199, [("us-1", True, "on_demand"), ("us-1", True, "spot")], kind="baremetal"),
    shape("alpha", "H100x8", "H100", 8, 1434, [("us-1", False, "on_demand")]),
    shape("gamma", "L40S", "L40S", 1, 80, [("us-2", True, "on_demand")], vram=48),
    shape("delta", "A100_80Gx2", "A100_80G", 2, 250, [("us-3", True, "on_demand")], kind="container"),
]}
KEYS = {"ssh_keys": [{"id": "key-1", "name": "laptop", "is_default": False, "public_key": "ssh-ed25519 AAAA-should-never-be-printed"},
                     {"id": "key-2", "name": "Managed", "is_default": True, "public_key": "ssh-ed25519 BBBB"}]}


class Fake:
    def __init__(self, types=TYPES, keys=KEYS):
        self.types, self.keys, self.calls = types, keys, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if path.startswith("/v1/instances/types"):
            return self.types
        if path == "/v1/sshkeys":
            return self.keys
        raise AssertionError(f"unexpected call {method} {path}")

    def query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.calls[-1][1]).query).items()}


def provider(**kw):
    fake = Fake(**kw)
    return ShadeformProvider(http=fake), fake


# ---- the provider ------------------------------------------------------------------------------------------------------

def test_available_on_demand_offers_cheapest_first_one_per_region():
    p, _ = provider()
    got = [(o.vendor, o.region, o.hourly_usd) for o in p.offers(OfferQuery())]
    assert got == [("gamma", "us-2", 0.8), ("alpha", "us-1", 1.99), ("delta", "us-3", 2.5), ("beta", "eu-1", 3.3)]


def test_spot_capacity_is_never_offered():
    p, _ = provider()
    assert [o.region for o in p.offers(OfferQuery(vendor="alpha", available_only=False))] == ["us-1", "us-1"]       # on-demand H100 and H100x8
    assert all(o.vendor == "alpha" and o.instance_type in ("H100", "H100x8") for o in p.offers(OfferQuery(vendor="alpha", available_only=False)))


def test_all_includes_sold_out_and_marks_them():
    p, _ = provider()
    sold_out = [o for o in p.offers(OfferQuery(available_only=False)) if not o.available]
    assert {(o.instance_type, o.region) for o in sold_out} == {("H100", "eu-2"), ("H100x8", "us-1")}


def test_fields_come_through():
    p, _ = provider()
    o = p.offers(OfferQuery(vendor="beta"))[0]
    assert (o.instance_type, o.gpu_type, o.num_gpus, o.vram_gb, o.vcpus, o.memory_gb, o.storage_gb, o.kind, o.boot_seconds) == \
        ("H100", "H100", 1, 80, 16, 192, 1000, "vm", 600)
    assert o.os_options == [OS_A, OS_B] and o.region_name == "EU-1" and o.interconnect == "pcie"


@pytest.mark.parametrize("query,expected", [
    (OfferQuery(gpu_type="h100"), {"eu-1", "us-1"}),                         # case does not matter
    (OfferQuery(vendor="BETA"), {"eu-1"}),
    (OfferQuery(region="us-2"), {"us-2"}),
    (OfferQuery(num_gpus=2), {"us-3"}),
    (OfferQuery(instance_type="l40s"), {"us-2"}),
    (OfferQuery(kind="baremetal"), {"us-1"}),
    (OfferQuery(max_price=2.0), {"us-2", "us-1"}),
    (OfferQuery(gpu_type="H100", num_gpus=8), set()),                        # the only 8-GPU box is sold out
])
def test_filters(query, expected):
    p, _ = provider()
    assert {o.region for o in p.offers(query)} == expected


def test_a_filter_the_server_ignores_does_not_widen_the_answer():
    """The fake returns every shape whatever the query says, like a server that ignores a parameter."""
    p, fake = provider()
    assert {o.vendor for o in p.offers(OfferQuery(vendor="gamma", region="us-2"))} == {"gamma"}
    assert fake.query()["cloud"] == "gamma"


def test_the_server_is_asked_to_narrow_and_sort():
    p, fake = provider()
    p.offers(OfferQuery(gpu_type="H100", num_gpus=8, vendor="alpha", instance_type="H100x8"))
    assert fake.query() == {"sort": "price", "available": "true", "gpu_type": "H100", "num_gpus": "8", "shade_instance_type": "H100x8", "cloud": "alpha"}
    p.offers(OfferQuery(available_only=False))
    assert "available" not in fake.query()


def test_query_values_are_url_encoded():
    p, fake = provider()
    p.offers(OfferQuery(gpu_type="a&cloud=evil b"))
    assert fake.calls[-1][1].count("&cloud=") == 0 and "a%26cloud%3Devil+b" in fake.calls[-1][1]


def test_a_shape_missing_optional_fields_is_still_listed():
    p, _ = provider(types={"instance_types": [{"cloud": "x", "shade_instance_type": "H100", "hourly_price": 100,
                                               "availability": [{"region": "r", "available": True}]}]})
    o = p.offers(OfferQuery())[0]
    assert (o.vendor, o.region, o.hourly_usd, o.num_gpus, o.os_options, o.kind) == ("x", "r", 1.0, 0, [], "")


def test_api_errors_are_plain_messages():
    def refuse(method, path, body=None):
        raise ApiError(401, "nope")
    with pytest.raises(PackError, match="rejected the API key"):
        ShadeformProvider(http=refuse).offers(OfferQuery())

    def boom(method, path, body=None):
        raise ApiError(500, '{"error": "backend down"}')
    with pytest.raises(PackError, match="could not list instance types.*backend down"):
        ShadeformProvider(http=boom).offers(OfferQuery())


def test_ssh_keys_never_carry_key_material():
    p, _ = provider()
    keys = p.ssh_keys()
    assert [(k.id, k.name, k.is_default) for k in keys] == [("key-1", "laptop", False), ("key-2", "Managed", True)]
    assert "AAAA" not in repr(keys)


def test_the_pack_check_still_refuses_with_a_menu_that_points_at_offers():
    p, _ = provider()
    spec = providers.HostSpec(name="x", instance_type="H100", vendor="alpha", region="nowhere")
    with pytest.raises(PackError, match=r"not available on alpha in nowhere.*vendor=alpha region=us-1 \$1.99/h.*labagent offers --type H100"):
        p.validate(spec)


# ---- the commands ------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    fake = Fake()
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=fake))
    return fake


def test_offers_json_is_a_stable_machine_shape(api, capsys):
    assert cli.main(["offers", "--gpu", "H100", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {"cloud", "fetched_at", "count", "returned", "offers"} and doc["cloud"] == "shadeform" and doc["count"] == 2
    first = doc["offers"][0]
    assert set(first) == {"instance_type", "vendor", "region", "region_name", "gpu_type", "num_gpus", "vram_gb", "vcpus", "memory_gb", "storage_gb",
                          "interconnect", "kind", "hourly_usd", "available", "boot_seconds", "max_labs", "os_options", "launch"}
    assert first["hourly_usd"] == 1.99 and first["max_labs"] == 7
    assert first["launch"] == {"cloud": "shadeform", "instance_type": "H100", "vendor": "alpha", "region": "us-1"}


def test_the_launch_block_is_what_the_pack_takes():
    from labagent.packs.gpu_notebooks.pack import GpuNotebooks
    names = {p.name for p in GpuNotebooks().params}
    o = ShadeformProvider(http=Fake()).offers(OfferQuery())[0]
    assert set(offers_cli.launch_params("shadeform", o)) <= names


def test_max_labs_is_only_estimated_for_known_gpus(api, capsys):
    cli.main(["offers", "--json", "--all"])
    by = {(o["instance_type"], o["region"]): o["max_labs"] for o in json.loads(capsys.readouterr().out)["offers"]}
    assert by[("H100", "us-1")] == 7 and by[("H100x8", "us-1")] == 56 and by[("A100_80Gx2", "us-3")] == 14 and by[("L40S", "us-2")] is None


def test_limit_and_counts(api, capsys):
    cli.main(["offers", "--json", "--limit", "1"])
    doc = json.loads(capsys.readouterr().out)
    assert (doc["count"], doc["returned"], len(doc["offers"])) == (4, 1, 1)
    assert doc["offers"][0]["vendor"] == "gamma"


def test_table_shows_the_next_command_and_the_os_images(api, capsys):
    assert cli.main(["offers", "--gpu", "H100"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and OS_A in out and f"{OS_A}, {OS_B}" in out
    assert "--param cloud=shadeform --param instance_type=H100 --param vendor=alpha --param region=us-1" in out
    assert out.index("alpha") < out.index("beta")                              # cheapest first


def test_table_hides_the_stock_column_until_all(api, capsys):
    cli.main(["offers", "--gpu", "H100"])
    assert "STOCK" not in capsys.readouterr().out
    cli.main(["offers", "--gpu", "H100", "--all"])
    out = capsys.readouterr().out
    assert "STOCK" in out and "H100x8" in out


def test_table_says_how_many_more_exist(api, capsys):
    cli.main(["offers", "--limit", "1"])
    assert "3 more not shown" in capsys.readouterr().out


def test_no_match_is_not_an_error(api, capsys):
    assert cli.main(["offers", "--gpu", "H100", "--gpus", "8"]) == 0
    assert "No offers match right now" in capsys.readouterr().out
    assert cli.main(["offers", "--gpu", "H100", "--gpus", "8", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["offers"] == []


def test_control_characters_from_the_api_never_reach_the_terminal(monkeypatch, capsys):
    evil = {"instance_types": [shape("al\x1b[2Jpha", "H100", "H100", 1, 100, [("us\x07-1", True, "on_demand")])]}
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=Fake(types=evil)))
    cli.main(["offers"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "al?[2Jpha" in out


def test_the_next_command_is_safe_to_paste_into_a_shell(monkeypatch, capsys):
    evil = {"instance_types": [shape("a;touch${IFS}pwned", "H100", "H100", 1, 100, [("us-1", True, "on_demand")])]}
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=Fake(types=evil)))
    cli.main(["offers"])
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("labagent launch"))
    assert "'vendor=a;touch${IFS}pwned'" in line                               # one quoted word, not a command


def test_ssh_keys_table_and_json(api, capsys):
    assert cli.main(["ssh-keys"]) == 0
    out = capsys.readouterr().out
    assert "key-1" in out and "laptop" in out and "Managed" in out and "AAAA" not in out and "BBBB" not in out
    assert cli.main(["ssh-keys", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ssh_keys"] == [{"id": "key-1", "name": "laptop", "is_default": False}, {"id": "key-2", "name": "Managed", "is_default": True}]
    assert "AAAA" not in json.dumps(doc)


def test_no_keys_in_the_account(monkeypatch, capsys):
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=Fake(keys={"ssh_keys": []})))
    assert cli.main(["ssh-keys"]) == 0
    assert "No SSH keys" in capsys.readouterr().out


@pytest.mark.parametrize("argv,message", [
    (["offers", "--cloud", "nowhere"], "unknown cloud"),
    (["offers", "--limit", "0"], "--limit must be 1 or more"),
    (["offers", "--gpus", "0"], "--gpus must be 1 or more"),
])
def test_bad_input_exits_2_with_a_message(api, capsys, argv, message):
    assert cli.main(argv) == 2
    assert message in capsys.readouterr().err


def test_a_missing_key_exits_2_and_never_prints_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    assert cli.main(["offers"]) == 2
    assert "no Shadeform API key" in capsys.readouterr().err


def test_a_rejected_key_exits_2(monkeypatch, capsys):
    def refuse(method, path, body=None):
        raise ApiError(403, "forbidden")
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=refuse))
    assert cli.main(["ssh-keys"]) == 2
    assert "rejected the API key" in capsys.readouterr().err


def test_the_commands_are_not_mistaken_for_plain_english(api, capsys):
    """Anything that is not a known command goes to the language model front end, so both names must be in KNOWN."""
    assert {"offers", "ssh-keys"} <= cli.KNOWN
