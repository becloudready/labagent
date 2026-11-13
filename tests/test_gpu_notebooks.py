"""gpu-notebooks: the provider layer and the host lifecycle, against a fake Shadeform API (no network, no key, no cost)."""
import json
import os
import urllib.error

import pytest

from labagent.packs import registry
from labagent.packs.base import load_state, run_pack
from labagent.packs.gpu_notebooks import providers
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, host_name, host_tag
from labagent.packs.gpu_notebooks.providers import ApiError, HostSpec, ShadeformProvider, read_secret

KEY = "sf-test-key-0123456789"
TYPES = {"instance_types": [
    {"shade_instance_type": "H100", "cloud": "massedcompute", "hourly_price": 273,
     "configuration": {"os_options": ["ubuntu22.04_cuda13.0_shade_os"]},
     "availability": [{"region": "desmoines-usa-1", "available": True, "rental_type": "on_demand"}]},
    {"shade_instance_type": "H100", "cloud": "othercloud", "hourly_price": 199,
     "configuration": {"os_options": ["ubuntu22.04_cuda12.2_shade_os"]},
     "availability": [{"region": "eu-1", "available": True, "rental_type": "on_demand"},
                      {"region": "eu-2", "available": False, "rental_type": "on_demand"}]},
]}


class FakeShadeform:
    """Just enough of the API. `statuses` is the sequence the info endpoint reports for a new instance."""

    def __init__(self, statuses=("pending", "pending", "active")):
        self.instances = {}
        self.calls = []
        self.statuses = list(statuses)
        self.n = 0

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/v1/instances/types"):
            return TYPES
        if method == "POST" and path == "/v1/instances/create":
            self.n += 1
            iid = f"inst-{self.n}"
            self.instances[iid] = {"id": iid, "name": body["name"], "tags": body.get("tags", []), "polls": 0, "status": "pending", "body": body}
            return {"id": iid}
        if method == "GET" and path == "/v1/instances":
            return {"instances": [self._view(i) for i in self.instances.values()]}
        if method == "GET" and path.endswith("/info"):
            iid = path.split("/")[3]
            if iid not in self.instances:
                raise ApiError(404, "not found")
            i = self.instances[iid]
            if i["status"] != "deleting":
                i["status"] = self.statuses[min(i["polls"], len(self.statuses) - 1)]
                i["polls"] += 1
            return self._view(i)
        if method == "POST" and path.endswith("/delete"):
            iid = path.split("/")[3]
            if iid not in self.instances:
                raise ApiError(404, "not found")
            self.instances[iid]["status"] = "deleting"
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    @staticmethod
    def _view(i):
        active = i["status"] == "active"
        return {"id": i["id"], "name": i["name"], "tags": i["tags"], "status": i["status"], "ip": "203.0.113.7" if active else None,
                "ssh_user": "shadeform" if active else None, "ssh_port": 22 if active else None}

    def creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/v1/instances/create"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    return tmp_path


def launch_params(**kw):
    return {"cloud": "massedcompute", "region": "desmoines-usa-1", "os": "ubuntu22.04_cuda13.0_shade_os", "ssh_key_id": "key-1", **kw}


def svc(api, **extra):
    prov = ShadeformProvider(http=api)
    return {"provider": lambda pid: prov, "sleep": lambda s: None, **extra}


def run(action, api, name="t", params=None, **extra):
    return run_pack(GpuNotebooks(), action, name, params if params is not None else launch_params(), services=svc(api, **extra))


# ---- registration and dry run ----

def test_registered_and_dry_run_touches_no_provider(home):
    assert "gpu-notebooks" in registry.all_packs()
    assert registry.resolve("gpu-labs").id == "gpu-notebooks"

    def boom(_):
        raise AssertionError("dry run must not build a provider or need a key")
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(), dry_run=True, services={"provider": boom})
    assert [i["step"] for i in env.items] == ["preflight", "host"] and env.status == "ok"


def test_unknown_provider_and_bad_hours_are_plain_errors(home):
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(provider="nope"), services={"provider": providers.make})
    assert env.status == "failed" and "unknown provider 'nope'" in json.dumps(env.to_dict())
    env = run("launch", FakeShadeform(), params=launch_params(hours=100))
    assert env.status == "failed" and "between 1 and 72" in json.dumps(env.to_dict())


# ---- credentials ----

def test_secret_from_bare_line_keyvalue_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    f = tmp_path / "credentials"
    f.write_text(f"[other]\nzzz\n[shadeform]\n{KEY}\n")
    f.chmod(0o600)
    assert read_secret("shadeform", "SHADEFORM_API_KEY", f) == (KEY, [])
    f.write_text(f"[shadeform]\napi_key = \"{KEY}\"\n")
    assert read_secret("shadeform", "SHADEFORM_API_KEY", f)[0] == KEY
    f.write_text("[aws]\nx\n")
    assert read_secret("shadeform", "SHADEFORM_API_KEY", f)[0] is None
    monkeypatch.setenv("SHADEFORM_API_KEY", "from-env")
    assert read_secret("shadeform", "SHADEFORM_API_KEY", f)[0] == "from-env"


def test_world_readable_credentials_file_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    f = tmp_path / "credentials"
    f.write_text(f"[shadeform]\n{KEY}\n")
    f.chmod(0o644)
    value, warnings = read_secret("shadeform", "SHADEFORM_API_KEY", f)
    assert value == KEY and "chmod 600" in warnings[0] and KEY not in warnings[0]


def test_missing_key_is_a_clear_error(home):
    with pytest.raises(Exception, match="no Shadeform API key"):
        ShadeformProvider()


# ---- offer validation ----

def test_validate_returns_price_and_lists_options_when_unavailable(home):
    p = ShadeformProvider(http=FakeShadeform())
    spec = HostSpec(name="x", instance_type="H100", cloud="massedcompute", region="desmoines-usa-1", os_image="ubuntu22.04_cuda13.0_shade_os")
    assert p.validate(spec)["hourly_cents"] == 273
    with pytest.raises(Exception) as e:
        p.validate(HostSpec(name="x", instance_type="H100", cloud="massedcompute", region="nowhere"))
    assert "othercloud region=eu-1 $1.99/h" in str(e.value)               # cheapest first, so the user can pick
    with pytest.raises(Exception, match="set cloud and region"):
        p.validate(HostSpec(name="x", instance_type="H100"))
    with pytest.raises(Exception, match="not offered"):
        p.validate(HostSpec(name="x", instance_type="H100", cloud="massedcompute", region="desmoines-usa-1", os_image="windows"))


# ---- launch and destroy ----

def test_create_request_matches_the_documented_call(home):
    api = FakeShadeform()
    env = run("launch", api, name="demo")
    assert env.status == "ok", env.to_dict()
    (_, _, body), = api.creates()
    assert body["cloud"] == "massedcompute" and body["region"] == "desmoines-usa-1" and body["shade_instance_type"] == "H100"
    assert body["os"] == "ubuntu22.04_cuda13.0_shade_os" and body["shade_cloud"] is True and body["ssh_key_id"] == "key-1"
    assert body["name"] == host_name("demo") and host_tag("demo") in body["tags"]
    assert body["auto_delete"]["date_threshold"].endswith("Z")           # the VM cannot outlive `hours`
    out = env.summary["outputs"]
    assert out["ip"] == "203.0.113.7" and out["host_id"] == "inst-1" and out["hourly_cents"] == 273


def test_optional_fields_are_left_out_not_sent_as_null(home):
    api = FakeShadeform()
    run("launch", api, params={"cloud": "massedcompute", "region": "desmoines-usa-1"})
    (_, _, body), = api.creates()
    assert "os" not in body and "ssh_key_id" not in body


def test_rerun_after_success_creates_nothing_more(home):
    api = FakeShadeform()
    run("launch", api)
    env = run("launch", api)
    assert len(api.creates()) == 1 and env.summary["skipped"] >= 1


def test_timeout_keeps_the_id_and_rerun_resumes_without_a_second_vm(home):
    api = FakeShadeform(statuses=("pending",))
    ticks = iter(range(0, 100000, 400))
    env = run("launch", api, clock=lambda: next(ticks))
    assert env.status == "failed" and "not active yet" in json.dumps(env.to_dict())
    assert load_state("gpu-notebooks", "t")["host_id"] == "inst-1"       # saved before the wait
    api.statuses = ["active"]
    env = run("launch", api)
    assert env.status == "ok" and len(api.creates()) == 1


def test_a_crash_between_create_and_save_is_adopted_by_name_and_tag(home):
    api = FakeShadeform()
    api("POST", "/v1/instances/create", {"name": host_name("t"), "tags": ["labagent", host_tag("t")]})   # created, state never saved
    env = run("launch", api)
    assert env.status == "ok" and len(api.creates()) == 1 and env.summary["outputs"]["host_id"] == "inst-1"


def test_an_instance_with_the_same_name_but_no_tag_is_never_adopted_or_deleted(home):
    api = FakeShadeform()
    api("POST", "/v1/instances/create", {"name": host_name("t"), "tags": []})       # someone else's VM
    env = run("launch", api)
    assert len(api.creates()) == 2 and env.summary["outputs"]["host_id"] == "inst-2"
    mine = run("destroy", api)
    assert mine.status == "ok" and api.instances["inst-1"]["status"] == "pending" and api.instances["inst-2"]["status"] == "deleting"


def test_provider_error_state_is_a_failure(home):
    env = run("launch", FakeShadeform(statuses=("pending", "error")))
    assert env.status == "failed" and "reports the VM as 'error'" in json.dumps(env.to_dict())


def test_destroy_deletes_only_this_launch_and_is_repeatable(home):
    api = FakeShadeform()
    run("launch", api, name="a")
    run("launch", api, name="b")
    env = run("destroy", api, name="a")
    assert env.status == "ok" and api.instances["inst-1"]["status"] == "deleting" and api.instances["inst-2"]["status"] != "deleting"
    assert load_state("gpu-notebooks", "a") == {}
    assert run("destroy", api, name="a").status == "ok"                  # nothing left: still fine


def test_destroy_after_a_lost_state_file_finds_the_vm_by_name_and_tag(home):
    api = FakeShadeform()
    api("POST", "/v1/instances/create", {"name": host_name("t"), "tags": ["labagent", host_tag("t")]})
    assert run("destroy", api).status == "ok" and api.instances["inst-1"]["status"] == "deleting"


def test_delete_of_a_vm_the_provider_already_removed_is_not_an_error(home):
    api = FakeShadeform()
    run("launch", api)
    del api.instances["inst-1"]
    assert run("destroy", api).status == "ok"


# ---- secrets ----

def test_the_key_never_reaches_state_envelope_or_errors(home, monkeypatch):
    seen = []

    def spying_http(key):
        inner = FakeShadeform()

        def call(m, p, b=None):
            seen.append(json.dumps([m, p, b]))
            return inner(m, p, b)
        return call
    monkeypatch.setenv("SHADEFORM_API_KEY", KEY)
    monkeypatch.setattr(providers, "make_http", spying_http)
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(), services={"provider": providers.make, "sleep": lambda s: None})
    assert env.status == "ok", env.to_dict()
    blob = json.dumps(env.to_dict()) + json.dumps(load_state("gpu-notebooks", "t")) + "".join(seen)
    assert KEY not in blob


def test_auth_rejection_names_the_fix_not_the_key(home):
    def deny(m, p, b=None):
        raise ApiError(401, "unauthorized")
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(), services={"provider": lambda pid: ShadeformProvider(http=deny)})
    assert env.status == "failed" and "rejected the API key" in json.dumps(env.to_dict())
