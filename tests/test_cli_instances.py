"""First-class --cloud/--gpu/--ttl, the local instance record, `destroy NAME`, `status` and the text-to-sql-h100 alias. No network, no cloud."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from labagent import cli, instances
from labagent.packs import registry
from labagent.packs.base import Pack, Param, Step

SEEN = []


class Box(Pack):
    id, title, summary = "fake-box", "Fake", "a fake rented box"
    params = [Param("cloud", "shadeform"), Param("instance_type", "H100"), Param("hours", 4), Param("mode", "x")]

    def deploy_steps(self, ctx):
        return [Step("go", "go", lambda c: SEEN.append(("launch", dict(c.params))) or {})]

    def destroy_steps(self, ctx):
        return [Step("rm", "rm", lambda c: SEEN.append(("destroy", dict(c.params))) or {})]


class Plain(Pack):
    id, title, summary = "fake-plain", "Plain", "no cloud settings"
    params = [Param("fail", False)]

    def deploy_steps(self, ctx):
        return [Step("go", "go", lambda c: {})]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.setattr(registry, "all_packs", lambda: {"fake-box": Box(), "fake-plain": Plain(), **{k: v for k, v in registry.builtin().items() if k == "gpu-notebooks-container"}})
    SEEN.clear()
    return tmp_path


def run(*argv):
    return cli.main(list(argv) + ["--output", "-"])


# ---- 1. flags map to params ----

def test_cloud_gpu_ttl_become_the_pack_parameters(capsys):
    assert run("launch", "fake-box", "--name", "a", "--cloud", "runpod", "--gpu", "H100x8", "--ttl", "90m") == 0
    _, p = SEEN[0]
    assert (p["cloud"], p["instance_type"], p["hours"]) == ("runpod", "H100x8", 2)          # 90 minutes rounds up to 2 hours


def test_the_flags_win_over_param_and_param_still_works():
    assert run("launch", "fake-box", "--name", "a", "--param", "cloud=lambda", "--param", "mode=y", "--cloud", "runpod", "--param", "instance_type=A100", "--gpu", "H100") == 0
    _, p = SEEN[0]
    assert (p["cloud"], p["instance_type"], p["mode"]) == ("runpod", "H100", "y")
    saved = instances.get("a")["params"]
    assert saved["cloud"] == "runpod" and saved["gpu"] == "H100"


def test_a_pack_without_the_setting_is_refused_by_name(capsys):
    assert run("launch", "fake-plain", "--name", "a", "--gpu", "H100") == 2
    assert "no gpu setting" in capsys.readouterr().err and instances.get("a") is None


def test_a_bad_ttl_is_refused_before_anything_runs(capsys):
    assert run("launch", "fake-box", "--name", "a", "--ttl", "90") == 2
    assert "ttl" in capsys.readouterr().err and SEEN == []


@pytest.mark.parametrize("text, minutes", [("90m", 90), ("2h", 120), ("1d", 1440), ("1.5h", 90), ("30s", 1)])
def test_ttl_parsing(text, minutes):
    assert instances.parse_ttl(text) == minutes


# ---- 2. state and destroy ----

def test_launch_saves_the_instance_and_destroy_by_name_needs_no_pack_or_cloud(home):
    assert run("launch", "fake-box", "--name", "demo2", "--cloud", "runpod", "--gpu", "H100", "--ttl", "2h") == 0
    f = home / "instances.json"
    e = json.loads(f.read_text())["demo2"]
    assert e["pack"] == "fake-box" and e["ttl_minutes"] == 120 and e["name"] == "demo2"
    assert e["params"]["cloud"] == "runpod" and datetime.fromisoformat(e["created_at"]).tzinfo is not None
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert run("destroy", "demo2") == 0
    kind, p = SEEN[-1]
    assert kind == "destroy" and p["cloud"] == "runpod" and p["instance_type"] == "H100"      # recalled, not re-typed
    assert instances.get("demo2") is None


def test_the_old_destroy_syntax_merges_saved_params_and_explicit_ones_win():
    run("launch", "fake-box", "--name", "a", "--cloud", "runpod", "--param", "mode=saved")
    assert run("destroy", "fake-box", "--name", "a", "--param", "mode=mine") == 0
    _, p = SEEN[-1]
    assert (p["cloud"], p["mode"]) == ("runpod", "mine")


def test_destroy_with_an_unknown_name_and_no_record_is_the_old_behavior(capsys):
    assert run("destroy", "fake-box", "--name", "never") == 0
    assert SEEN[-1][1]["cloud"] == "shadeform"                                                 # pack defaults, as before


def test_a_dry_run_and_a_failed_destroy_leave_the_record_alone():
    assert run("launch", "fake-box", "--name", "d", "--dry-run") == 0 and instances.get("d") is None
    run("launch", "fake-box", "--name", "k")
    run("destroy", "k", "--dry-run")
    assert instances.get("k") is not None


def test_a_rerun_keeps_the_first_created_at():
    run("launch", "fake-box", "--name", "r")
    first = instances.get("r")["created_at"]
    run("launch", "fake-box", "--name", "r", "--ttl", "1h")
    e = instances.get("r")
    assert e["created_at"] == first and e["ttl_minutes"] == 60


def test_a_corrupt_record_never_breaks_a_launch(home, capsys):
    (home / "instances.json").write_text("{nope")
    assert run("launch", "fake-box", "--name", "c") == 0
    assert "instances.json" in capsys.readouterr().err


# ---- 3. status ----

def test_status_prints_a_table(home, capsys):
    now = datetime.now(timezone.utc)
    instances.record("demo2", "fake-box", {"cloud": "runpod", "gpu": "H100"}, 120, now=now - timedelta(minutes=30))
    instances.record("old", "fake-box", {"cloud": "lambda", "instance_type": "H100"}, 60, now=now - timedelta(minutes=90))
    instances.record("free", "fake-plain", {}, None, now=now - timedelta(hours=3, minutes=5))
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NAME", "PACK", "CLOUD", "GPU", "ELAPSED", "TTL", "REMAINING"]
    row = {l.split()[0]: l.split() for l in lines[1:]}
    assert row["demo2"][:4] == ["demo2", "fake-box", "runpod", "H100"] and row["demo2"][4] == "30m" and row["demo2"][5] == "1h30m"
    assert row["old"][3] == "H100" and row["old"][-1] == "expired"
    assert row["free"][4] == "3h05m" and row["free"][-1] == "-"


def test_status_shows_the_pack_defaults_when_the_launch_did_not_set_them(capsys):
    run("launch", "fake-box", "--name", "d")
    capsys.readouterr()
    cli.main(["status"])
    assert capsys.readouterr().out.splitlines()[1].split()[:4] == ["d", "fake-box", "shadeform", "H100"]


def test_status_empty_and_json(capsys):
    assert cli.main(["status"]) == 0 and "No instances" in capsys.readouterr().out
    run("launch", "fake-box", "--name", "j", "--cloud", "runpod", "--ttl", "1h")
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)["instances"]
    assert items[0]["name"] == "j" and items[0]["cloud"] == "runpod" and items[0]["ttl_minutes"] == 60 and "ttl_remaining_minutes" in items[0]


# ---- 4. the alias ----

def test_text_to_sql_h100_resolves_to_the_container_pack_and_is_listed(capsys):
    assert registry.resolve("text-to-sql-h100").id == "gpu-notebooks-container"
    assert cli.main(["packs"]) == 0
    out = capsys.readouterr().out
    assert any(l.startswith("text-to-sql-h100") and "alias of gpu-notebooks-container" in l for l in out.splitlines())
    assert any(l.startswith("gpu-notebooks-container") for l in out.splitlines())


def test_the_alias_takes_the_new_flags_in_a_dry_run(capsys):
    assert cli.main(["launch", "text-to-sql-h100", "--name", "q", "--cloud", "runpod", "--gpu", "H100", "--ttl", "2h", "--dry-run", "--output", "-"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "launch:gpu-notebooks-container" and out["status"] == "ok"
