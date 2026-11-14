"""gpu-notebooks: the provider layer and the host lifecycle, against a fake Shadeform API (no network, no key, no cost)."""
import csv
import fnmatch
import json
import os
import re
import subprocess
import urllib.error
from pathlib import Path

import pytest

from labagent.packs import registry
from labagent.packs.base import load_state, run_pack
from labagent.packs.gpu_notebooks import providers
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, credentials_path, default_hostname, host_name, host_tag, login_path
from tests.test_packaging import patterns
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
    return {"vendor": "massedcompute", "region": "desmoines-usa-1", "os": "ubuntu22.04_cuda13.0_shade_os", "ssh_key_id": "key-1", **kw}


class FakeSsh:
    """Stands in for the VM: answers the few commands the pack sends, and remembers every command and its stdin."""

    def __init__(self, down_for=0, slices=7, anon=403, gpu="1 NVIDIA H100 PCIe MIG 1g.10gb 9984", fail=None):
        self.calls, self.csv, self.down_for, self.slices, self.anon, self.gpu, self.fail = [], None, down_for, slices, anon, gpu, fail
        self.args = None

    def __call__(self, host, user, port, key_file, known_hosts):
        self.args = (host, user, port, key_file, known_hosts)
        return self

    def run(self, command, stdin=None, timeout=900):
        self.calls.append((command, stdin))
        if command == "true":
            if self.down_for > 0:
                self.down_for -= 1
                return 255, "", "ssh: connect to host: Connection refused"
            return 0, "", ""
        if self.fail and command.endswith(self.fail):
            return 1, "", "ERROR: cannot pull the image\n"
        if command == "echo $HOME":
            return 0, "/home/shadeform\n", ""
        if "credentials.csv" in command and stdin:
            self.csv = stdin
        if command.endswith("start-labs.sh access"):
            self.password = (stdin or "").rstrip("\n")
            return 0, "HOSTNAME=x\nLOGIN_USER=x\nPROMPT=set\n", ""
        if command.endswith("start-labs.sh partition"):
            return 0, f"SLICES={self.slices}\nPROFILE=1g.10gb\nGPU_NAME=NVIDIA H100 PCIe\nGPU_COUNT=1\n", ""
        if command.endswith("start-labs.sh verify"):
            rows = [l.split(",") for l in (self.csv or "").splitlines()[1:]]
            return 0, "".join(f"LAB {r[0]} up=200 anon={self.anon} token=200 | {self.gpu}\n" for r in rows), ""
        return 0, "", ""

    def commands(self):
        return [c for c, _ in self.calls]


def svc(api, ssh=None, public=200, **extra):
    prov = ShadeformProvider(http=api) if api is not None else None
    return {"provider": lambda pid: prov, "sleep": lambda s: None, "ssh": ssh or FakeSsh(), "http_get": lambda url, verify: public, **extra}


def run(action, api, name="t", params=None, log=None, **extra):
    return run_pack(GpuNotebooks(), action, name, params if params is not None else launch_params(), services=svc(api, **extra), log=log)


# ---- registration and dry run ----

def test_registered_and_dry_run_touches_no_provider(home):
    assert "gpu-notebooks" in registry.all_packs()
    assert registry.resolve("gpu-labs").id == "gpu-notebooks"

    def boom(_):
        raise AssertionError("dry run must not build a provider or need a key")
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(), dry_run=True, services={"provider": boom})
    assert [i["step"] for i in env.items] == ["preflight", "host", "setup", "access", "labs", "verify", "report"] and env.status == "ok"


def test_unknown_provider_and_bad_hours_are_plain_errors(home):
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(cloud="nope"), services={"provider": providers.make})
    assert env.status == "failed" and "unknown cloud 'nope'" in json.dumps(env.to_dict())
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
    spec = HostSpec(name="x", instance_type="H100", vendor="massedcompute", region="desmoines-usa-1", os_image="ubuntu22.04_cuda13.0_shade_os")
    assert p.validate(spec)["hourly_cents"] == 273
    with pytest.raises(Exception) as e:
        p.validate(HostSpec(name="x", instance_type="H100", vendor="massedcompute", region="nowhere"))
    assert "vendor=othercloud region=eu-1 $1.99/h" in str(e.value)               # cheapest first, so the user can pick
    with pytest.raises(Exception, match="set vendor and region"):
        p.validate(HostSpec(name="x", instance_type="H100"))
    with pytest.raises(Exception, match="not offered"):
        p.validate(HostSpec(name="x", instance_type="H100", vendor="massedcompute", region="desmoines-usa-1", os_image="windows"))


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
    run("launch", api, params={"vendor": "massedcompute", "region": "desmoines-usa-1"})
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
    ssh = FakeSsh()
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(), services={**svc(None, ssh=ssh), "provider": providers.make})
    assert env.status == "ok", env.to_dict()
    blob = json.dumps(env.to_dict()) + json.dumps(load_state("gpu-notebooks", "t")) + "".join(seen) + "".join(ssh.commands())
    assert KEY not in blob


def test_auth_rejection_names_the_fix_not_the_key(home):
    def deny(m, p, b=None):
        raise ApiError(401, "unauthorized")
    env = run_pack(GpuNotebooks(), "launch", "t", launch_params(), services={**svc(None), "provider": lambda pid: ShadeformProvider(http=deny)})
    assert env.status == "failed" and "rejected the API key" in json.dumps(env.to_dict())


# ---- the labs: MIG slices, one container each, URLs and logins ----

def tokens_in(ssh):
    return [l.split(",")[2] for l in ssh.csv.splitlines()[1:]]


def test_launch_ends_with_one_url_and_login_per_lab(home):
    ssh, logs = FakeSsh(), []
    env = run("launch", FakeShadeform(), ssh=ssh, log=logs.append)
    assert env.status == "ok", env.to_dict()
    cmds = ssh.commands()
    order = [next(i for i, c in enumerate(cmds) if key in c) for key in ("labs.env", "tee /opt/labagent/start-labs.sh", "start-labs.sh partition",
                                                                         "start-labs.sh access", "credentials.csv", "start-labs.sh labs", "start-labs.sh proxy",
                                                                         "start-labs.sh install_service", "start-labs.sh verify")]
    assert order == sorted(order)
    rows = list(csv.DictReader(open(credentials_path("t"), encoding="utf-8")))
    assert [r["lab"] for r in rows] == [f"lab-{i:02d}" for i in range(1, 8)]
    assert rows[0]["login_url"] == f"https://203-0-113-7.sslip.io/lab-01/lab?token={rows[0]['token']}"
    report = "\n".join(logs)
    for r in rows:
        assert r["login_url"] in report                                   # the terminal shows every URL with its login
    assert oct(credentials_path("t").stat().st_mode & 0o777) == "0o600"
    assert "lab-01,8801," in ssh.csv and "lab-07,8807," in ssh.csv         # one port per lab, bound to the VM's localhost by the script
    out = env.summary["outputs"]
    assert out["labs"]["lab-03"]["url"] == "https://203-0-113-7.sslip.io/lab-03/lab" and out["public_ok"] is True
    assert not any(r["token"] in json.dumps(env.to_dict()) + json.dumps(load_state("gpu-notebooks", "t")) for r in rows)   # envelope and state: no logins


def test_tokens_travel_on_stdin_never_on_a_command_line(home):
    ssh = FakeSsh()
    run("launch", FakeShadeform(), ssh=ssh)
    for tok in tokens_in(ssh):
        assert not any(tok in c for c in ssh.commands())
    assert all(len(t) == 32 for t in tokens_in(ssh)) and len(set(tokens_in(ssh))) == 7


def test_settings_file_carries_the_choices(home):
    ssh = FakeSsh()
    run("launch", FakeShadeform(), ssh=ssh, params=launch_params(profile="2g.20gb", tls="internal", image="example/lab:1", domain="example.com"))
    env_file = next(stdin for c, stdin in ssh.calls if "labs.env" in c)
    assert "PROFILE='2g.20gb'" in env_file and "TLS='internal'" in env_file and "IMAGE='example/lab:1'" in env_file
    assert "DOMAIN='example.com'" in env_file and "LABS_DIR='/home/shadeform/labs'" in env_file


def test_labs_param_limits_the_count_and_ssh_uses_the_key_file(home):
    ssh = FakeSsh()
    env = run("launch", FakeShadeform(), ssh=ssh, params=launch_params(labs=3, ssh_key_file="/keys/id"))
    assert env.status == "ok" and len(ssh.csv.splitlines()) == 4
    assert ssh.args[0] == "203.0.113.7" and ssh.args[1] == "shadeform" and ssh.args[3] == "/keys/id" and ssh.args[4].endswith(".known_hosts")


def test_rerun_keeps_the_same_logins_so_learners_stay_logged_in(home):
    first, second = FakeSsh(), FakeSsh()
    api = FakeShadeform()
    run("launch", api, ssh=first)
    run("launch", api, ssh=second)
    assert tokens_in(first) == tokens_in(second) and len(api.creates()) == 1


def test_ssh_that_comes_up_late_is_waited_for_and_one_that_never_does_is_explained(home):
    late = FakeSsh(down_for=3)
    assert run("launch", FakeShadeform(), ssh=late).status == "ok"
    ticks = iter(range(0, 10 ** 6, 100))
    env = run("launch", FakeShadeform(), name="u", ssh=FakeSsh(down_for=10 ** 6), clock=lambda: next(ticks))
    assert env.status == "failed" and "ssh_key_file" in json.dumps(env.to_dict())


def test_a_remote_failure_shows_the_scripts_own_message_without_any_token(home):
    ssh = FakeSsh(fail="start-labs.sh labs")
    env = run("launch", FakeShadeform(), ssh=ssh)
    text = json.dumps(env.to_dict())
    assert env.status == "failed" and "cannot pull the image" in text and "starting the lab containers" in text
    assert not any(t in text for t in tokens_in(ssh))


@pytest.mark.parametrize("kw,why", [({"anon": 200}, "WITHOUT its token"), ({"gpu": "2 NVIDIA H100 PCIe 81559"}, "exactly one GPU slice")])
def test_a_lab_that_is_open_or_sees_more_than_its_slice_fails_the_launch(home, kw, why):
    env = run("launch", FakeShadeform(), ssh=FakeSsh(**kw))
    assert env.status == "failed" and why in json.dumps(env.to_dict())


def test_labs_not_reachable_from_the_internet_are_a_warning_with_a_tunnel(home):
    logs, ticks = [], iter(range(0, 10 ** 6, 100))
    env = run("launch", FakeShadeform(), public=None, log=logs.append, clock=lambda: next(ticks))
    assert env.status == "ok" and env.summary["outputs"]["public_ok"] is False
    text = "\n".join(logs)
    assert "not reachable from here" in text and "ssh -N -L 8801:localhost:8801" in text and "-L 8807:localhost:8807" in text


def test_no_slices_is_a_plain_failure(home):
    env = run("launch", FakeShadeform(), ssh=FakeSsh(slices=0))
    assert env.status == "failed" and "no MIG slices" in json.dumps(env.to_dict())


@pytest.mark.parametrize("bad", [{"profile": "1g.10gb; rm -rf /"}, {"image": "x y"}, {"tls": "yes"}, {"labs": 100}, {"labs": "many"}, {"domain": "a b"}])
def test_lab_settings_are_validated_before_anything_is_created(home, bad):
    api = FakeShadeform()
    env = run("launch", api, params=launch_params(**bad))
    assert env.status == "failed" and api.creates() == []


def test_destroy_also_removes_the_local_logins(home):
    api = FakeShadeform()
    run("launch", api)
    assert credentials_path("t").exists()
    assert run("destroy", api).status == "ok" and not credentials_path("t").exists()


# ---- the installer script that runs on the VM ----

SETUP = Path(providers.__file__).with_name("setup.sh")


def test_installer_script_is_valid_bash_and_shipped_in_the_wheel():
    assert subprocess.run(["bash", "-n", str(SETUP)], capture_output=True).returncode == 0
    assert any(fnmatch.fnmatch("packs/gpu_notebooks/setup.sh", pat) for pat in patterns())


def test_installer_script_never_prints_a_token_or_puts_one_in_a_command_line():
    text = SETUP.read_text()
    assert not re.search(r"echo[^\n]*\$\{?token", text)
    assert "-e JUPYTER_TOKEN \\" in text and 'JUPYTER_TOKEN="$token" docker run' in text     # value comes from the environment, not argv
    assert "--security-opt no-new-privileges" in text and "127.0.0.1:$port:8888" in text    # not exposed except through the proxy


# ---- host name and the shared login ----

def test_host_name_comes_from_the_launch_name_unless_given():
    assert default_hostname("Nvidia H100_Hackathon Nov") == "nvidia-h100-hackathon-nov" and default_hostname("***") == "gpu-labs"
    assert len(default_hostname("a" * 90)) <= 63


def settings(ssh):
    return next(stdin for c, stdin in ssh.calls if "labs.env" in c)


def test_the_vm_gets_the_launch_name_as_host_name_and_a_shared_login(home):
    ssh, logs = FakeSsh(), []
    env = run("launch", FakeShadeform(), name="nvidia-h100-hackathon", ssh=ssh, log=logs.append, params=launch_params(domain="example.com"))
    assert env.status == "ok", env.to_dict()
    assert "LAB_HOSTNAME='nvidia-h100-hackathon'" in settings(ssh) and "LOGIN_USER='labagent'" in settings(ssh) and "LOGIN_SUDO='yes'" in settings(ssh)
    row = next(csv.DictReader(open(login_path("nvidia-h100-hackathon"), encoding="utf-8")))
    assert row["user"] == "labagent" and len(row["password"]) == 20 and row["password"] == ssh.password
    assert oct(login_path("nvidia-h100-hackathon").stat().st_mode & 0o777) == "0o600"
    assert f"ssh labagent@example.com   password: {row['password']}" in "\n".join(logs)
    assert "that is root" in "\n".join(logs)
    blob = json.dumps(env.to_dict()) + json.dumps(load_state("gpu-notebooks", "nvidia-h100-hackathon")) + "".join(ssh.commands())
    assert row["password"] not in blob                                    # stdin only: not in a command, the envelope or the state
    assert env.summary["outputs"]["hostname"] == "nvidia-h100-hackathon" and env.summary["outputs"]["login_user"] == "labagent"


def test_explicit_host_name_login_user_and_no_sudo(home):
    ssh = FakeSsh()
    run("launch", FakeShadeform(), ssh=ssh, params=launch_params(hostname="paris-mig-workshop", login_user="student", login_sudo=False))
    assert "LAB_HOSTNAME='paris-mig-workshop'" in settings(ssh) and "LOGIN_USER='student'" in settings(ssh) and "LOGIN_SUDO='no'" in settings(ssh)


def test_the_shared_password_survives_a_rerun(home):
    a, b = FakeSsh(), FakeSsh()
    api = FakeShadeform()
    run("launch", api, ssh=a)
    run("launch", api, ssh=b)
    assert a.password == b.password


def test_no_login_user_still_sets_the_host_name(home):
    ssh = FakeSsh()
    env = run("launch", FakeShadeform(), ssh=ssh, params=launch_params(login_user=""))
    assert env.status == "ok" and "LOGIN_USER=''" in settings(ssh) and not login_path("t").exists()


@pytest.mark.parametrize("bad", [{"hostname": "Bad_Name"}, {"hostname": "-x"}, {"login_user": "root"}, {"login_user": "shadeform"}, {"login_user": "A b"}])
def test_host_name_and_user_are_validated_before_anything_is_created(home, bad):
    api = FakeShadeform()
    assert run("launch", api, params=launch_params(**bad)).status == "failed" and api.creates() == []


def test_destroy_removes_the_shared_login_file_too(home):
    api = FakeShadeform()
    run("launch", api)
    assert login_path("t").exists()
    run("destroy", api)
    assert not login_path("t").exists()


def test_access_script_keeps_the_password_off_command_lines_and_sets_the_host_name_for_good():
    text = SETUP.read_text()
    assert 'IFS= read -r pw' in text and not re.search(r"echo[^\n]*\$pw[^\n]*\n?", text.replace('echo "$LOGIN_USER:$pw" | chpasswd', ""))
    assert "preserve_hostname: true" in text and "hostnamectl set-hostname" in text
    assert "sshd -t" in text and "Match User" in text                       # password login for that one user only, and the config is validated first


def test_access_script_sets_a_user_and_host_prompt_for_login_and_interactive_shells():
    text = SETUP.read_text()
    assert r"PS1='\[\e[1;32m\]\u@\h" in text                                  # user@host, coloured
    assert "$home/.bash_profile" in text and '. "$HOME/.bashrc"' in text         # login shells load it too
    assert "# >>> labagent prompt >>>" in text and "# <<< labagent prompt <<<" in text and "sed -i '/# >>> labagent prompt >>>/" in text   # replaced, never duplicated


def test_a_vm_that_does_not_confirm_the_prompt_fails_the_step(home):
    ssh = FakeSsh()
    plain = ssh.run

    def no_prompt(command, stdin=None, timeout=900):
        return (0, "HOSTNAME=x\n", "") if command.endswith("start-labs.sh access") else plain(command, stdin, timeout)
    ssh.run = no_prompt
    env = run("launch", FakeShadeform(), ssh=ssh)
    assert env.status == "failed" and "did not confirm the shell prompt" in json.dumps(env.to_dict())


def test_access_script_repairs_an_account_made_by_hand():
    text = SETUP.read_text()
    assert 'usermod -s /bin/bash "$LOGIN_USER"' in text                      # an existing user with /bin/sh
    assert 'install -d -m 755 -o "$LOGIN_USER"' in text and "/etc/skel/$f" in text   # a home that is missing or owned by root
    assert "runuser -u" in text and "PROMPT=set" in text                    # the prompt is proven by a real login shell, not assumed


def test_yes_no_params_mean_the_same_from_the_command_line_and_from_a_file(home):
    from labagent.packs.gpu_notebooks.pack import flag
    assert [flag(v) for v in (True, "true", "True", "yes", "on", "1")] == [True] * 6
    assert [flag(v) for v in (False, "false", "False", "no", "off", "", None)] == [False] * 7
    ssh, logs = FakeSsh(), []
    run("launch", FakeShadeform(), ssh=ssh, log=logs.append, params=launch_params(login_sudo="false"))     # the text a --param flag gives
    assert "LOGIN_SUDO='no'" in settings(ssh)
    assert next(csv.DictReader(open(login_path("t"), encoding="utf-8")))["sudo"] == "no" and "that is root" not in "\n".join(logs)
