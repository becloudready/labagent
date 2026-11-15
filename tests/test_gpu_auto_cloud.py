"""cloud=auto: the cheapest machine across every cloud that has a key here. All clouds are fakes: no network."""
import json
import shutil

import pytest

from labagent.compute import providers
from labagent.compute.lambda_cloud import LambdaProvider
from labagent.compute.providers import ShadeformProvider
from labagent.packs.base import state_dir
from tests import test_gpu_notebooks as base
from tests.test_gpu_lambda import FakeLambda
from tests.test_gpu_notebooks import FakeShadeform, home, run  # noqa: F401  (home is a fixture)


def auto(**kw):
    return {"cloud": "auto", "instance_type": "H100", "location": "any", "ssh_key_file": "/keys/mine", **kw}      # the stub file is the private half of key 1 in both


def clouds(**have):
    """The provider factory the pack uses: a cloud with no key here raises the way a real one does."""
    def make(pid):
        if pid not in have:
            raise providers.ComputeError(f"no {pid} API key")
        return have[pid]
    return make


@pytest.fixture
def two():
    sf, lam = FakeShadeform(), FakeLambda()
    return sf, lam, clouds(shadeform=ShadeformProvider(http=sf), **{"lambda": LambdaProvider(http=lam)})


def test_the_cheapest_cloud_is_rented_and_the_others_are_left_alone(home, two):
    sf, lam, make = two
    lines = []
    env = run("launch", sf, params=auto(), provider=make, log=lines.append)
    assert env.status == "ok", env.to_dict()
    assert env.summary["outputs"]["cloud"] == "shadeform"                       # $1.99 against Lambda's $3.29
    assert len(sf.creates()) == 1 and lam.launches() == []
    text = "\n".join(lines)
    assert "auto: shadeform $1.99/h, lambda $3.29/h -> shadeform" in text                # cheapest first, and the winner named
    assert "skipped nebius: no nebius API key" in text                          # a cloud with no key here is named, not fatal


def test_a_dearer_cloud_wins_when_the_cheaper_one_has_nothing_in_budget(home, two, monkeypatch):
    sf, lam, make = two
    dear = json.loads(json.dumps(base.TYPES))
    for t in dear["instance_types"]:
        t["hourly_price"] = 999                                                  # Shadeform's H100 is $9.99 now
    monkeypatch.setattr(base, "TYPES", dear)
    env = run("launch", sf, params=auto(), provider=make)
    assert env.status == "ok", env.to_dict()
    assert env.summary["outputs"]["cloud"] == "lambda"
    assert len(lam.launches()) == 1 and sf.creates() == []


def test_the_price_ceiling_applies_to_every_cloud(home, two):
    sf, lam, make = two
    env = run("launch", sf, params=auto(max_price=1.0), provider=make)
    assert env.status == "failed" and "cloud=auto found no cloud to rent H100" in json.dumps(env.to_dict())
    assert sf.creates() == [] and lam.launches() == []                           # nothing was rented


def test_a_cloud_that_does_not_have_our_ssh_key_is_skipped_even_when_it_is_cheapest(home, two):
    sf, lam, make = two
    sf.keys = [k for k in sf.keys if k["id"] != "key-1"]                          # Shadeform has no key with this file's fingerprint
    lines = []
    env = run("launch", sf, params=auto(), provider=make, log=lines.append)
    assert env.status == "ok", env.to_dict()
    assert env.summary["outputs"]["cloud"] == "lambda"
    assert any("auto: skipped shadeform:" in l for l in lines)
    assert sf.creates() == []


def test_vendor_and_region_cannot_be_combined_with_auto(home, two):
    sf, lam, make = two
    env = run("launch", sf, params=auto(vendor="massedcompute", region="desmoines-usa-1"), provider=make)
    assert env.status == "failed" and "leave vendor and region out" in json.dumps(env.to_dict())
    assert sf.creates() == []


def test_destroy_goes_to_the_cloud_that_was_chosen(home, two):
    sf, lam, make = two
    assert run("launch", sf, params=auto(), provider=make).status == "ok"
    env = run("destroy", sf, params=auto(), provider=make)
    assert env.status == "ok", env.to_dict()
    assert all(i["status"] == "deleting" for i in sf.instances.values()) and lam.terminated() == []


def test_destroy_with_a_lost_state_finds_the_vm_by_name_in_whichever_cloud_has_it(home, two):
    sf, lam, make = two
    env = run("launch", sf, params=auto(), provider=make)
    assert env.status == "ok", env.to_dict()
    shutil.rmtree(state_dir(), ignore_errors=True)                                 # the state is gone: nothing remembers the cloud
    state_dir().mkdir(parents=True, exist_ok=True)
    env = run("destroy", sf, params=auto(), provider=make)
    assert env.status == "ok", env.to_dict()
    assert all(i["status"] == "deleting" for i in sf.instances.values())
    assert lam.terminated() == []                                                  # only the VM with our name and tag, in the cloud that held it


def test_destroy_with_nothing_anywhere_is_a_calm_no_op(home, two):
    sf, lam, make = two
    env = run("destroy", sf, params=auto(), provider=make)
    assert env.status == "ok" and sf.instances == {} and lam.terminated() == []
