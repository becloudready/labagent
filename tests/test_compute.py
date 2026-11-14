"""`labagent compute`: the clouds listing, the heading that says whose answer a table is, the JSON failure documents, and the layering."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from labagent import cli, compute
from labagent.compute import providers
from labagent.compute.providers import ApiError, ShadeformProvider
from tests.test_gpu_offers import Fake

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(monkeypatch):
    fake = Fake()
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=fake))
    return fake


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.delenv("SHADEFORM_API_KEY", raising=False)
    return tmp_path


# ---- clouds ----

def test_clouds_json_says_what_each_cloud_can_do_and_never_shows_a_key(home, monkeypatch, capsys):
    monkeypatch.setenv("SHADEFORM_API_KEY", "sk-secret-value-123")
    assert cli.main(["compute", "clouds", "--json"]) == 0
    raw = capsys.readouterr().out
    doc = json.loads(raw)
    assert (doc["schema"], doc["status"]) == ("compute.clouds/1", "ok")
    sf = next(c for c in doc["clouds"] if c["id"] == "shadeform")
    assert sf["title"] == "Shadeform" and sf["capabilities"] == ["offers", "ssh_keys", "launch"]
    assert sf["credentials"] == {"configured": True, "source": "environment", "env_var": "SHADEFORM_API_KEY", "file": sf["credentials"]["file"], "section": "shadeform"}
    assert "sk-secret-value-123" not in raw


def test_clouds_knows_where_the_key_comes_from_and_when_it_is_missing(home, monkeypatch, capsys):
    assert cli.main(["compute", "clouds", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["clouds"][0]["credentials"]["configured"] is False
    (home / "credentials").write_text("[shadeform]\nkey = sk-file-secret\n")
    (home / "credentials").chmod(0o600)
    assert cli.main(["compute", "clouds", "--json"]) == 0
    raw = capsys.readouterr().out
    assert json.loads(raw)["clouds"][0]["credentials"]["source"] == "credentials file" and "sk-file-secret" not in raw


def test_clouds_table_names_the_fix_when_a_key_is_missing(home, capsys):
    assert cli.main(["compute", "clouds"]) == 0
    out = capsys.readouterr().out
    assert "Shadeform" in out and "offers, ssh-keys, launch" in out and "missing: set SHADEFORM_API_KEY or [shadeform] in" in out


# ---- the table says whose answer it is ----

def test_every_table_opens_with_the_cloud_the_kind_of_answer_and_the_time(api, capsys):
    cli.main(["compute", "offers", "--cloud", "shadeform", "--gpu", "H100"])
    first = capsys.readouterr().out.splitlines()[0].replace("\u00b7", "-")
    assert first.startswith("Shadeform - GPU offers - on-demand, available now, 2 offers, cheapest first - fetched ") and first.endswith("Z")
    cli.main(["compute", "offers", "--cloud", "shadeform", "--all", "--limit", "1"])
    assert "including sold out, 1 of 6 offers, cheapest first" in capsys.readouterr().out.splitlines()[0]
    cli.main(["compute", "ssh-keys", "--cloud", "shadeform"])
    assert capsys.readouterr().out.splitlines()[0].replace("\u00b7", "-").startswith("Shadeform - SSH keys - 2 in this account - fetched ")


def test_an_empty_answer_still_says_whose_it_is(api, capsys):
    cli.main(["compute", "offers", "--cloud", "shadeform", "--gpu", "H100", "--gpus", "8"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].replace("\u00b7", "-").startswith("Shadeform - GPU offers") and "0 offers" in out[0] and "cheapest" not in out[0] and out[1].startswith("No offers match")


def test_the_next_step_hint_names_the_cloud(api, capsys):
    cli.main(["compute", "offers", "--cloud", "shadeform", "--gpu", "H100"])
    assert "labagent compute ssh-keys --cloud shadeform" in capsys.readouterr().out


# ---- JSON documents ----

def test_ssh_keys_json_has_the_same_envelope_as_offers(api, capsys):
    assert cli.main(["compute", "ssh-keys", "--cloud", "shadeform", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {"schema", "status", "cloud", "cloud_title", "fetched_at", "count", "warnings", "ssh_keys"}
    assert (doc["schema"], doc["status"], doc["count"]) == ("compute.ssh-keys/1", "ok", 2)


@pytest.mark.parametrize("argv,schema,message", [
    (["compute", "offers", "--json"], "compute.offers/1", "--cloud is required"),
    (["compute", "offers", "--cloud", "nowhere", "--json"], "compute.offers/1", "unknown cloud 'nowhere'"),
    (["compute", "offers", "--cloud", "shadeform", "--limit", "0", "--json"], "compute.offers/1", "--limit must be 1 or more"),
    (["compute", "ssh-keys", "--json"], "compute.ssh-keys/1", "--cloud is required"),
])
def test_a_failure_with_json_is_a_json_document_and_nothing_else(api, capsys, argv, schema, message):
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc == {"schema": schema, "status": "failed", "error": doc["error"]} and message in doc["error"]
    assert captured.err == ""


def test_a_rejected_key_with_json_is_a_failure_document(monkeypatch, capsys):
    def refuse(method, path, body=None):
        raise ApiError(401, "no")
    monkeypatch.setitem(providers.PROVIDERS, "shadeform", lambda: ShadeformProvider(http=refuse))
    assert cli.main(["compute", "offers", "--cloud", "shadeform", "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "failed" and "rejected the API key" in doc["error"]


def test_a_bad_flag_is_refused_before_any_call_to_the_cloud(api, capsys):
    assert cli.main(["compute", "offers", "--cloud", "shadeform", "--gpus", "0"]) == 2
    assert api.calls == []


def test_the_old_top_level_commands_are_gone(capsys):
    """offers and ssh-keys were never released. Typing them now goes to the plain-English front end, not to a half-working command."""
    assert "offers" not in cli.KNOWN and "ssh-keys" not in cli.KNOWN


# ---- the library ----

def test_the_library_answers_without_any_printing(api, capsys):
    got = compute.make("shadeform").offers(compute.OfferQuery(gpu_type="H100"))
    assert [o.vendor for o in got] == ["alpha", "beta"] and capsys.readouterr().out == ""
    assert [c["id"] for c in compute.catalog()] == ["shadeform"]


def test_a_compute_error_is_a_pack_error_so_packs_show_it_unchanged():
    from labagent.packs.base import PackError
    assert issubclass(compute.ComputeError, PackError)


def test_the_compute_layer_does_not_import_a_pack():
    code = "import sys, labagent.compute, labagent.compute.cli; print(sorted(m for m in sys.modules if m.startswith('labagent.packs.') and m != 'labagent.packs.base'))"
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]", out
