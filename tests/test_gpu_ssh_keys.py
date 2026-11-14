"""The SSH key a GPU launch uses, and what happens when a launch fails: nothing may keep billing.

No network and no cloud: the Shadeform API is a fake. The ssh-keygen tests run the real command and skip where it is missing.
"""
import json
import shutil
import subprocess
import stat

import pytest

from labagent import cli
from labagent.compute import cli as compute_cli, keys as keyfiles, providers
from labagent.compute.errors import ComputeError
from labagent.compute.providers import ApiError, ShadeformProvider, fingerprint_of
from labagent.packs.base import PackContext, load_state, state_path
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, credentials_path, host_name, host_tag
from tests.test_gpu_notebooks import FP1, FP2, FakeShadeform, FakeSsh, PUB1, home, launch_params, run  # noqa: F401  (home is a fixture)

needs_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")
def ticks():
    """A clock that jumps 400 s every time it is read, so every wait times out at once."""
    it = iter(range(0, 1000000, 400))
    return lambda: next(it)


def deleted(api):
    return [c[1].split("/")[3] for c in api.calls if c[0] == "POST" and c[1].endswith("/delete")]


# ---- the key is checked before anything is rented ----------------------------------------------------------------------

def test_a_launch_without_ssh_key_id_is_refused_before_anything_is_rented(home):
    api = FakeShadeform()
    p = launch_params()
    p.pop("ssh_key_id")
    env = run("launch", api, params=p)
    assert env.status == "failed" and "no SSH key to log in with" in env.error and "ssh-key-add --cloud all" in env.error
    assert api.creates() == []


def test_a_key_id_that_is_not_in_the_account_is_refused_with_the_keys_that_are(home):
    api = FakeShadeform()
    env = run("launch", api, params=launch_params(ssh_key_id="nope"))
    assert env.status == "failed" and "nope is not in your shadeform account" in env.error and "key-1 labagent-test" in env.error
    assert api.creates() == []


def test_a_private_file_that_is_the_other_half_of_a_different_key_rents_nothing(home):
    api = FakeShadeform()
    env = run("launch", api, params=launch_params(ssh_key_id="key-2", ssh_key_file="/keys/mine"))           # the stub file is the half of key-1
    assert env.status == "failed" and api.creates() == []
    assert "mine" in env.error and FP1 in env.error and FP2 in env.error and "Nothing was rented" in env.error
    assert env.summary["cleanup"]["action"] == "nothing"                                                      # no VM, and the result says so


def test_a_matching_file_is_recorded_as_verified_and_shown_in_the_result(home):
    api = FakeShadeform()
    env = run("launch", api, params=launch_params(ssh_key_file="/keys/mine"))
    assert env.status == "ok", env.to_dict()
    out = env.summary["outputs"]
    assert (out["ssh_key_id"], out["ssh_key_name"], out["ssh_key_fingerprint"], out["ssh_key_verified"]) == ("key-1", "labagent-test", FP1, True)
    assert "/keys" not in json.dumps(env.to_dict())                                                           # the path stays on the machine


def test_the_key_file_comes_from_the_environment_when_the_job_cannot_set_it(home, monkeypatch):
    monkeypatch.setenv("LABAGENT_SSH_KEY_FILE", "/keys/from-env")
    seen = {}
    ssh = FakeSsh()
    env = run("launch", FakeShadeform(), params=launch_params(), ssh=ssh, private_fingerprint=lambda f: seen.setdefault("file", f) and FP1)
    assert env.status == "ok" and seen["file"] == "/keys/from-env" and ssh.args[3] == "/keys/from-env"
    assert env.summary["outputs"]["ssh_key_verified"] is True


def test_without_a_key_file_the_launch_goes_ahead_but_says_the_key_is_not_checked(home, monkeypatch):
    monkeypatch.delenv("LABAGENT_SSH_KEY_FILE", raising=False)
    lines = []
    env = run("launch", FakeShadeform(), params=launch_params(), log=lines.append)
    assert env.status == "ok" and env.summary["outputs"]["ssh_key_verified"] is False
    assert any("not checked" in l for l in lines)


def test_a_key_the_cloud_shows_no_public_half_for_cannot_be_checked_but_is_not_refused(home):
    api = FakeShadeform()
    api.keys[0]["public_key"] = ""
    lines = []
    env = run("launch", api, params=launch_params(ssh_key_file="/keys/mine"), log=lines.append)
    assert env.status == "ok" and env.summary["outputs"]["ssh_key_verified"] is False
    assert any("cannot be checked" in l for l in lines)


def test_a_job_may_not_choose_the_key_file_or_keep_a_failed_vm(home):
    from labagent.run import run_pack_job
    job = {"command": "launch", "pack": "gpu-notebooks", "name": "t", "run_id": "r", "mode": "dry-run"}
    for bad in ("ssh_key_file", "keep_on_failure"):
        refused = run_pack_job({**job, "params": {bad: "yes"}})
        assert refused.status == "failed" and bad in refused.error


# ---- a launch that fails deletes the VM ---------------------------------------------------------------------------------

def test_an_ssh_login_that_never_works_deletes_the_vm_and_says_so(home):
    api = FakeShadeform()
    env = run("launch", api, ssh=FakeSsh(down_for=10 ** 6), clock=ticks(), params=launch_params(ssh_key_file="/keys/mine"))
    assert env.status == "failed" and "step 'setup' failed: cannot log in to the VM over ssh" in env.error
    assert "labagent-test" in env.error and FP1 in env.error                                                  # which key was used, in the error itself
    assert deleted(api) == ["inst-1"] and api.instances["inst-1"]["status"] == "deleting"
    assert env.summary["cleanup"] == {"action": "deleted", "host_id": "inst-1"}
    assert not state_path("gpu-notebooks", "t").exists() and not credentials_path("t").exists()               # a rerun starts clean
    assert env.summary["outputs"]["ssh_key_id"] == "key-1" and env.summary["outputs"]["ssh_key_verified"] is True


def test_a_failure_in_any_later_step_deletes_the_vm_too(home):
    api = FakeShadeform()
    env = run("launch", api, ssh=FakeSsh(fail="start-labs.sh partition"))
    assert env.status == "failed" and "splitting the GPUs" in env.error
    assert deleted(api) == ["inst-1"] and env.summary["cleanup"]["action"] == "deleted"


def test_a_vm_that_never_becomes_active_is_deleted(home):
    api = FakeShadeform(statuses=("pending",))
    env = run("launch", api, clock=ticks())
    assert env.status == "failed" and "not active yet" in env.error
    assert deleted(api) == ["inst-1"] and env.summary["cleanup"]["action"] == "deleted"


def test_a_crash_between_create_and_save_is_also_cleaned_up_by_name_and_tag(home):
    api = FakeShadeform()
    api("POST", "/v1/instances/create", {"name": host_name("t"), "tags": ["labagent", host_tag("t")]})       # created, state never saved
    note = GpuNotebooks().on_failure(PackContext("gpu-notebooks", "t", GpuNotebooks().resolve(launch_params()), state={},
                                                 services={"provider": lambda pid: ShadeformProvider(http=api)}), "host")
    assert note == {"action": "deleted", "host_id": "inst-1"}


def test_a_preflight_failure_has_nothing_to_clean_up_and_makes_no_delete_call(home):
    api = FakeShadeform()
    env = run("launch", api, params=launch_params(hours=100))
    assert env.status == "failed" and deleted(api) == [] and env.summary["cleanup"] == {"action": "nothing", "detail": "nothing was rented"}
    assert not any(c[0] == "GET" and c[1] == "/v1/instances" for c in api.calls)


def test_keep_on_failure_leaves_the_vm_and_says_how_to_stop_it(home):
    api = FakeShadeform()
    env = run("launch", api, ssh=FakeSsh(fail="start-labs.sh partition"), params=launch_params(keep_on_failure="yes"))
    assert env.status == "failed" and deleted(api) == []
    assert env.summary["cleanup"]["action"] == "kept" and "labagent destroy gpu-notebooks --name t" in env.summary["cleanup"]["detail"]
    assert load_state("gpu-notebooks", "t")["host_id"] == "inst-1"                                           # so destroy can find it


def test_when_the_delete_itself_fails_the_state_is_kept_and_the_error_is_loud(home):
    class Stuck(FakeShadeform):
        def __call__(self, method, path, body=None):
            if method == "POST" and path.endswith("/delete"):
                raise ApiError(500, '{"error": "cloud is down"}')
            return super().__call__(method, path, body)
    api = Stuck()
    env = run("launch", api, ssh=FakeSsh(fail="start-labs.sh partition"))
    assert env.status == "failed" and "splitting the GPUs" in env.error                                      # the real error is not hidden
    note = env.summary["cleanup"]
    assert note["action"] == "failed" and note["host_id"] == "inst-1" and "could not delete the VM" in note["detail"] and "labagent destroy" in note["detail"]
    assert load_state("gpu-notebooks", "t")["host_id"] == "inst-1"


def test_ctrl_c_still_deletes_the_vm(home):
    class Interrupted(FakeSsh):
        def run(self, command, stdin=None, timeout=900):
            if command == "echo $HOME":
                raise KeyboardInterrupt
            return super().run(command, stdin, timeout)
    api = FakeShadeform()
    env = run("launch", api, ssh=Interrupted())
    assert env.status == "failed" and "interrupted" in env.error
    assert deleted(api) == ["inst-1"] and env.summary["cleanup"]["action"] == "deleted"


def test_a_lab_that_was_already_working_is_not_deleted_by_a_failed_rerun(home):
    api = FakeShadeform()
    assert run("launch", api).status == "ok"
    assert load_state("gpu-notebooks", "t")["ready"] is True
    pack = GpuNotebooks()
    ctx = PackContext("gpu-notebooks", "t", pack.resolve(launch_params()), state=load_state("gpu-notebooks", "t"),
                      services={"provider": lambda pid: ShadeformProvider(http=api)})
    note = pack.on_failure(ctx, "labs")
    assert note["action"] == "kept" and deleted(api) == []


def test_destroy_still_removes_what_a_kept_vm_left(home):
    api = FakeShadeform()
    run("launch", api, ssh=FakeSsh(fail="start-labs.sh partition"), params=launch_params(keep_on_failure="yes"))
    env = run("destroy", api)
    assert env.status == "ok" and deleted(api) == ["inst-1"] and not state_path("gpu-notebooks", "t").exists()


# ---- fingerprints and the key command ------------------------------------------------------------------------------------

def test_a_fingerprint_is_sha256_of_the_key_blob_without_padding_and_junk_gives_nothing():
    fp = fingerprint_of(PUB1)
    assert fp.startswith("SHA256:") and "=" not in fp and len(fp) == len("SHA256:") + 43
    assert fingerprint_of(PUB1 + " and a longer comment") == fp and fingerprint_of("") == "" and fingerprint_of("ssh-ed25519") == ""
    assert fingerprint_of("ssh-ed25519 not*base64!") == "" and fingerprint_of(None) == ""


@needs_keygen
def test_our_fingerprint_is_what_ssh_keygen_prints(tmp_path):
    public, made = keyfiles.ensure_key(str(tmp_path / "k"), "test")
    shown = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "k.pub")], capture_output=True, text=True).stdout.split()[1]
    assert made and fingerprint_of(public) == shown == keyfiles.private_fingerprint(str(tmp_path / "k"))


@needs_keygen
def test_ensure_key_creates_a_private_file_only_the_owner_can_read_and_never_overwrites(tmp_path):
    path = tmp_path / "keys" / "k"
    public, made = keyfiles.ensure_key(str(path), "c")
    assert made and stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    before = path.read_bytes()
    again, made = keyfiles.ensure_key(str(path), "c")
    assert not made and again == public and path.read_bytes() == before


def test_a_missing_or_unreadable_key_file_is_a_plain_error(tmp_path):
    with pytest.raises(ComputeError, match="does not exist.*ssh-key-add"):
        keyfiles.public_half(str(tmp_path / "nope"))
    junk = tmp_path / "junk"
    junk.write_text("not a key")
    if shutil.which("ssh-keygen"):
        with pytest.raises(ComputeError):
            keyfiles.public_half(str(junk))


def test_adding_a_public_key_reuses_one_that_is_already_there(home):
    api = FakeShadeform()
    prov = ShadeformProvider(http=api)
    key, added = prov.add_ssh_key("again", PUB1)
    assert (key.id, added) == ("key-1", False) and [c for c in api.calls if c[1] == "/v1/sshkeys/add"] == []
    other = "ssh-ed25519 " + "QUJD" + " x"
    key, added = prov.add_ssh_key("new", other)
    assert added and key.name == "new" and key.fingerprint == fingerprint_of(other)
    (_, _, body), = [c for c in api.calls if c[1] == "/v1/sshkeys/add"]
    assert body == {"name": "new", "public_key": other}                                                       # exactly the public line: no private field
    with pytest.raises(ComputeError, match="not an OpenSSH public key"):
        prov.add_ssh_key("bad", "-----BEGIN OPENSSH PRIVATE KEY-----")


@needs_keygen
def test_the_ssh_key_add_command_makes_the_file_registers_only_the_public_half_and_is_repeatable(home, monkeypatch, capsys, tmp_path):
    api = FakeShadeform()
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=api))
    f = tmp_path / "keys" / "console"
    argv = ["compute", "ssh-key-add", "--cloud", "shadeform", "--name", "labagent-console", "--file", str(f), "--json"]
    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["schema"] == "compute.ssh-key/1" and first["status"] == "ok"
    assert (first["name"], first["key_file_created"], first["added_to_cloud"]) == ("labagent-console", True, True)
    assert first["fingerprint"] == keyfiles.private_fingerprint(str(f)) and first["ssh_key_id"] == "key-3"
    sent = [c[2] for c in api.calls if c[1] == "/v1/sshkeys/add"]
    assert len(sent) == 1 and set(sent[0]) == {"name", "public_key"} and "PRIVATE" not in json.dumps(sent)
    assert cli.main(argv) == 0                                                                                # again: same key, nothing new
    second = json.loads(capsys.readouterr().out)
    assert (second["ssh_key_id"], second["key_file_created"], second["added_to_cloud"]) == ("key-3", False, False)
    assert len([c for c in api.calls if c[1] == "/v1/sshkeys/add"]) == 1


def test_the_ssh_key_add_command_needs_a_cloud_and_a_sane_name(home, monkeypatch, capsys):
    assert cli.main(["compute", "ssh-key-add", "--name", "x", "--json"]) == 2
    assert "--cloud is required" in json.loads(capsys.readouterr().out)["error"]
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=FakeShadeform()))
    assert cli.main(["compute", "ssh-key-add", "--cloud", "shadeform", "--name", "bad;name", "--json"]) == 2
    assert "--name must be" in json.loads(capsys.readouterr().out)["error"]


def test_the_listing_shows_fingerprints_and_never_the_private_field(home, monkeypatch, capsys):
    api = FakeShadeform()
    api.keys[1]["private_key"] = "-----BEGIN OPENSSH PRIVATE KEY----- SECRET"
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=api))
    assert cli.main(["compute", "ssh-keys", "--cloud", "shadeform"]) == 0
    out = capsys.readouterr().out
    assert FP1 in out and FP2 in out and "SECRET" not in out and "PRIVATE" not in out
