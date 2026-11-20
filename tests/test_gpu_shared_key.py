"""One SSH key for every cloud: the launch finds it in each account by fingerprint, and `ssh-key-add --cloud all` puts it in every account.

No network and no cloud: both clouds are fakes. The ssh-keygen tests run the real command and skip where it is missing.
"""
import json
import shutil

import pytest

from labagent import cli
from labagent.compute import keys as keyfiles, nebius, providers
from labagent.compute.providers import ShadeformProvider
from labagent.compute.lambda_cloud import LambdaProvider
from labagent.packs.base import load_state
from tests.test_gpu_lambda import FakeLambda, pack_params
from tests.test_gpu_notebooks import FP1, FakeShadeform, FakeSsh, PUB1, home, launch_params, run  # noqa: F401  (home is a fixture)

needs_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")


def no_id(**kw):
    p = launch_params(**kw)
    p.pop("ssh_key_id")
    return p


def lambda_no_id(**kw):
    p = pack_params(**kw)
    p.pop("ssh_key_id")
    return p


# ---- a launch with no ssh_key_id finds the key by the fingerprint of the private file ------------------------------------

def test_without_an_id_the_key_in_the_account_with_the_files_fingerprint_is_used(home):
    api, lines = FakeShadeform(), []
    env = run("launch", api, params=no_id(ssh_key_file="/keys/mine"), log=lines.append)          # the stub file is the private half of key-1
    assert env.status == "ok", env.to_dict()
    (_, _, body), = api.creates()
    assert body["ssh_key_id"] == "key-1"                                                           # the host step got the id the preflight found
    out = env.summary["outputs"]
    assert (out["ssh_key_id"], out["ssh_key_name"], out["ssh_key_fingerprint"], out["ssh_key_verified"]) == ("key-1", "labagent-test", FP1, True)
    assert any("found in shadeform by the fingerprint of mine" in l for l in lines)
    assert load_state("gpu-notebooks", "t")["ssh_key_id"] == "key-1"


def test_the_same_private_file_works_on_lambda_where_the_id_is_another_one(home):
    shadeform, lam = FakeShadeform(), FakeLambda()
    assert run("launch", shadeform, params=no_id(ssh_key_file="/keys/mine")).status == "ok"
    env = run("launch", lam, name="l", params=lambda_no_id(ssh_key_file="/keys/mine"), provider=lambda pid: LambdaProvider(http=lam))
    assert env.status == "ok", env.to_dict()
    assert shadeform.creates()[0][2]["ssh_key_id"] == "key-1" and lam.launches()[0][2]["ssh_key_names"] == ["labagent-test"]      # one file, two clouds
    assert env.summary["outputs"]["ssh_key_id"] == "k1"                                           # Lambda's own id for the same key


def test_a_file_whose_key_is_not_in_the_account_rents_nothing_and_says_how_to_add_it(home):
    api = FakeShadeform()
    api.keys = [k for k in api.keys if k["id"] != "key-1"]                                         # only the account's default key is left
    env = run("launch", api, params=no_id(ssh_key_file="/keys/mine"))
    assert env.status == "failed" and api.creates() == []
    assert "mine" in env.error and FP1 in env.error and "not in your shadeform account" in env.error
    assert "ssh-key-add --cloud shadeform --name labagent --file /keys/mine" in env.error
    assert env.summary["cleanup"]["action"] == "nothing"


def test_the_shared_key_file_is_picked_up_from_its_usual_place(home, monkeypatch):
    monkeypatch.delenv("LABAGENT_SSH_KEY_FILE", raising=False)
    shared = keyfiles.default_key_file()
    assert shared.endswith("keys/labagent_ed25519") and str(home) in shared                       # under LABAGENT_HOME, one file for all clouds
    env = run("launch", FakeShadeform(), params=no_id())
    assert env.status == "failed" and "ssh-key-add --cloud all" in env.error                      # no file yet: told how to make it
    import os
    os.makedirs(os.path.dirname(shared))
    open(shared, "w").close()                                                                      # the file exists (its fingerprint is stubbed)
    ssh = FakeSsh()
    api = FakeShadeform()
    env = run("launch", api, params=no_id(), ssh=ssh)
    assert env.status == "ok", env.to_dict()
    assert api.creates()[0][2]["ssh_key_id"] == "key-1" and ssh.args[3] == shared


def test_an_explicit_id_still_wins_and_is_still_checked(home):
    env = run("launch", FakeShadeform(), params=launch_params(ssh_key_id="key-2", ssh_key_file="/keys/mine"))     # the file is the half of key-1
    assert env.status == "failed" and "is not the private half of key" in env.error


def test_a_job_cannot_pick_the_key_file_even_with_a_shared_key(home):
    from labagent.run import run_pack_job
    refused = run_pack_job({"command": "launch", "pack": "gpu-notebooks", "name": "t", "run_id": "r", "mode": "dry-run", "params": {"ssh_key_file": "/etc/shadow"}})
    assert refused.status == "failed" and "ssh_key_file" in refused.error


# ---- ssh-key-add --cloud all ---------------------------------------------------------------------------------------------

def factory(cls, api):
    """What PROVIDERS holds, but with a fake API behind it: the class's own facts (env_var, title...) stay, so `catalog()` still sees which keys are set."""
    def make():
        return cls(http=api)
    for attr in ("id", "title", "capabilities", "env_var", "key_file"):
        setattr(make, attr, getattr(cls, attr))
    return make


@pytest.fixture
def both(home, monkeypatch):
    sf, lam = FakeShadeform(), FakeLambda()
    monkeypatch.setenv("SHADEFORM_API_KEY", "sf-test-secret-1")
    monkeypatch.setenv("LAMBDA_API_KEY", "lam-test-secret-1")
    monkeypatch.setattr(nebius, "_installed_cli", lambda: None)                                    # whatever nebius login the machine running the tests has
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", factory(ShadeformProvider, sf))
    monkeypatch.setitem(providers.PROVIDERS, "lambda", factory(LambdaProvider, lam))
    return sf, lam


@needs_keygen
def test_one_command_adds_the_same_key_to_every_cloud_with_a_key_set(both, capsys):
    sf, lam = both
    argv = ["compute", "ssh-key-add", "--cloud", "all", "--name", "labagent", "--json"]
    assert cli.main(argv) == 0
    raw = capsys.readouterr().out
    doc = json.loads(raw)
    assert doc["schema"] == "compute.ssh-key-all/1" and doc["status"] == "ok" and doc["key_file_created"] is True
    assert doc["key_file"] == keyfiles.default_key_file()
    rows = {r["cloud"]: r for r in doc["clouds"]}
    assert (rows["shadeform"]["status"], rows["shadeform"]["added_to_cloud"], rows["shadeform"]["ssh_key_id"]) == ("ok", True, "key-3")
    assert (rows["lambda"]["status"], rows["lambda"]["added_to_cloud"], rows["lambda"]["ssh_key_id"]) == ("ok", True, "k3")
    assert doc["fingerprint"] == keyfiles.private_fingerprint(doc["key_file"])
    sent = [c[2] for c in sf.calls if c[1] == "/v1/sshkeys/add"] + [c[2] for c in lam.calls if c[0] == "POST" and c[1] == "/api/v1/ssh-keys"]
    assert len(sent) == 2 and sent[0]["public_key"] == sent[1]["public_key"] and all(set(b) == {"name", "public_key"} for b in sent)
    assert "PRIVATE" not in json.dumps(sent) and "secret" not in raw                               # the private half and the API keys stay out
    assert cli.main(argv) == 0                                                                     # again: one file, nothing added twice
    again = json.loads(capsys.readouterr().out)
    assert again["key_file_created"] is False and [r["added_to_cloud"] for r in again["clouds"] if r["status"] == "ok"] == [False, False]
    assert len([c for c in sf.calls if c[1] == "/v1/sshkeys/add"]) == 1


@needs_keygen
def test_after_that_a_launch_needs_no_id_on_either_cloud(both, home):
    assert cli.main(["compute", "ssh-key-add", "--cloud", "all", "--name", "labagent"]) == 0
    sf, lam = both
    ssh = FakeSsh()
    svc = dict(ssh=ssh, private_fingerprint=keyfiles.private_fingerprint)                          # the real file this time, not the stub
    assert run("launch", sf, params=no_id(), **svc).status == "ok"
    assert sf.creates()[0][2]["ssh_key_id"] == "key-3"
    env = run("launch", lam, name="l", params=lambda_no_id(), provider=lambda pid: LambdaProvider(http=lam), **svc)
    assert env.status == "ok", env.to_dict()
    assert lam.launches()[0][2]["ssh_key_names"] == ["labagent"]


@needs_keygen
def test_a_cloud_without_an_api_key_is_skipped_and_one_that_fails_does_not_stop_the_rest(both, monkeypatch, capsys):
    sf, lam = both
    monkeypatch.delenv("LAMBDA_API_KEY")
    assert cli.main(["compute", "ssh-key-add", "--cloud", "all", "--name", "labagent", "--json"]) == 0
    rows = {r["cloud"]: r for r in json.loads(capsys.readouterr().out)["clouds"]}
    assert rows["lambda"]["status"] == "skipped" and "LAMBDA_API_KEY" in rows["lambda"]["error"] and rows["shadeform"]["status"] == "ok"
    assert [c for c in lam.calls] == []                                                            # a skipped cloud is not called at all
    monkeypatch.setenv("LAMBDA_API_KEY", "lam-test-secret-1")
    from labagent.compute.providers import ApiError
    lam.fail[("POST", "/api/v1/ssh-keys")] = ApiError(500, "boom")
    assert cli.main(["compute", "ssh-key-add", "--cloud", "all", "--name", "labagent", "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "failed" and "lambda" in doc["error"]
    assert {r["cloud"]: r["status"] for r in doc["clouds"]} == {"lambda": "failed", "nebius": "skipped", "runpod": "skipped", "shadeform": "ok"}


@needs_keygen
def test_a_cloud_with_no_key_list_is_skipped_with_the_reason_whether_or_not_it_has_a_login(both, monkeypatch, capsys):
    argv = ["compute", "ssh-key-add", "--cloud", "all", "--name", "labagent", "--json"]
    assert cli.main(argv) == 0
    row = {r["cloud"]: r for r in json.loads(capsys.readouterr().out)["clouds"]}["nebius"]
    assert row["status"] == "skipped" and "NEBIUS_IAM_TOKEN" in row["error"]                      # no login: the usual "set the key" hint
    monkeypatch.setenv("NEBIUS_IAM_TOKEN", "nebius-test-secret")
    assert cli.main(argv) == 0
    row = {r["cloud"]: r for r in json.loads(capsys.readouterr().out)["clouds"]}["nebius"]
    assert row["status"] == "skipped" and "no key list" in row["error"] and "installed on each VM when it is created" in row["error"]


@needs_keygen
def test_the_text_output_lists_each_cloud_and_names_the_file(both, capsys):
    assert cli.main(["compute", "ssh-key-add", "--cloud", "all", "--name", "labagent"]) == 0
    out = capsys.readouterr().out
    assert "One key for every cloud: labagent" in out and "lambda" in out and "shadeform" in out and "added" in out
    assert "labagent_ed25519" in out and "no ssh_key_id to copy" in out


def test_with_no_api_key_anywhere_there_is_nowhere_to_add_it(home, monkeypatch, capsys):
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    monkeypatch.delenv("LAMBDA_API_KEY", raising=False)
    assert cli.main(["compute", "ssh-key-add", "--cloud", "all", "--name", "labagent", "--json"]) == 2
    assert "no cloud has an API key set" in json.loads(capsys.readouterr().out)["error"]
    assert not (home / "keys").exists()                                                            # and no key file was made for nothing


def test_the_name_is_checked_before_a_file_is_made_or_a_cloud_called(both, capsys):
    sf, lam = both
    assert cli.main(["compute", "ssh-key-add", "--cloud", "all", "--name", "bad;name", "--json"]) == 2
    assert "--name must be" in json.loads(capsys.readouterr().out)["error"]
    assert sf.calls == [] and lam.calls == []
