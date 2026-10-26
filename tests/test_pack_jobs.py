"""launch and destroy as job-contract commands: what the console's runner sends, and what comes back."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from labagent.cli import main
from labagent.packs import registry
from labagent.packs.base import Pack, PackError, Param, Step
from labagent.run import run_job


@pytest.fixture
def keys():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pem


def decrypt(priv, b64):
    return priv.decrypt(base64.b64decode(b64), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).decode()


class Fake(Pack):
    id, title, summary = "fake-ide", "Fake", "test"
    params = [Param("fail", False), Param("image", "auto"), Param("profile", None)]

    def deploy_steps(self, ctx):
        def make(c):
            c.mem["passwords"] = {"ana": "pw-ana-1", "ben": "pw-ben-2", "lost": "(lost: relaunch)"}
            c.state["learners"] = {"ana": {"url": "https://x/ana", "sandbox_id": "sb-1"}}
            if c.params["fail"]:
                raise PackError("boom after minting passwords")
            return {"host": "h.example"}
        return [Step("make", "make", make)]

    def destroy_steps(self, ctx):
        return [Step("rm", "remove", lambda c: {})]

    def outputs(self, state):
        return {"learners": {n: {"url": i["url"]} for n, i in state.get("learners", {}).items()}}


@pytest.fixture(autouse=True)
def fake_pack(monkeypatch, tmp_path):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.setattr(registry, "resolve", lambda name: Fake() if name == "fake-ide" else (_ for _ in ()).throw(KeyError(f"unknown pack '{name}'")))


def job(**kw):
    return {"run_id": "r1", "command": "launch", "pack": "fake-ide", "name": "demo", "params": {}, **kw}


def test_launch_returns_encrypted_passwords_and_urls_only(keys):
    priv, pem = keys
    env = run_job(None, job(secrets_pubkey_pem=pem))          # no AWS context needed
    assert env.status == "ok" and env.run_id == "r1"
    assert {s["user"]: decrypt(priv, s["ciphertext"]) for s in env.secrets} == {"ana": "pw-ana-1", "ben": "pw-ben-2"}   # '(lost...)' is not a secret
    text = json.dumps(env.to_dict())
    assert "pw-ana-1" not in text and "sb-1" not in text and "https://x/ana" in text


def test_without_a_public_key_nothing_secret_is_returned():
    env = run_job(None, job())
    assert env.secrets == [] and "pw-ana" not in json.dumps(env.to_dict())


def test_a_failed_launch_still_returns_the_passwords_it_already_minted(keys):
    priv, pem = keys
    env = run_job(None, job(params={"fail": True}, secrets_pubkey_pem=pem))
    assert env.status == "failed" and "boom" in env.error
    assert decrypt(priv, env.secrets[0]["ciphertext"]) == "pw-ana-1"


def test_destroy_and_dry_run():
    assert run_job(None, job(command="destroy")).status == "ok"
    dry = run_job(None, job(mode="dry-run"))
    assert dry.mode == "dry-run" and dry.items[0]["state"] == "planned"


@pytest.mark.parametrize("bad", [
    {"name": "Has Space"}, {"name": ""}, {"name": "x" * 40}, {"name": "-lead"},
    {"params": {"image": "/etc"}}, {"params": {"profile": "prod"}}, {"params": ["a"]},
    {"pack": "nope"}, {"params": {"unknown": 1}},
])
def test_bad_jobs_are_refused_without_running(bad):
    env = run_job(None, job(**bad))
    assert env.status == "failed" and env.error and env.secrets == []


def test_cli_run_executes_pack_jobs_without_aws(tmp_path, keys, capsys):
    _, pem = keys
    f = tmp_path / "job.json"
    f.write_text(json.dumps(job(secrets_pubkey_pem=pem)))
    out = tmp_path / "result.json"
    assert main(["run", "--job", str(f), "--output", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["status"] == "ok" and len(res["secrets"]) == 2
    f.write_text(json.dumps(job(params={"fail": True})))
    assert main(["run", "--job", str(f), "--output", str(out)]) == 2


def test_rag_ide_outputs_hide_sandbox_ids():
    from labagent.packs.rag_ide.pack import RagIde
    out = RagIde().outputs({"learners": {"ana": {"url": "https://u", "sandbox_id": "sb-9"}}, "host": "1.2.3.4.sslip.io", "credentials_file": "/home/x/c.csv"})
    assert out == {"learners": {"ana": {"url": "https://u"}}, "host": "1.2.3.4.sslip.io"}
