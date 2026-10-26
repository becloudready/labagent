"""Pack framework, the Databricks CLI wrapper and the shared login handling. Each pack has its own test file."""
import json

import pytest

from labagent.cli import main
from labagent.packs import base, registry
from labagent.packs.base import Pack, PackContext, PackError, Param, Step, run_pack
from labagent.packs.databricks.auth import login
from labagent.packs.databricks.cli import Dbx, redact


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    return tmp_path


class Hello(Pack):
    id, title, summary = "hello", "Hello", "demo"
    params = [Param("who", "world")]

    def deploy_steps(self, ctx):
        return [Step("greet", "say hi", lambda c: {"greeting": f"hi {c.params['who']}"})]


def test_adding_a_pack_needs_no_framework_change(home):
    env = run_pack(Hello(), "launch", "x", {"who": "lab"}, services={})
    assert env.status == "ok" and base.load_state("hello", "x")["greeting"] == "hi lab"


def test_unknown_param_is_rejected(home):
    env = run_pack(Hello(), "launch", "demo", {"nope": 1}, services={})
    assert env.status == "failed" and "unknown parameter" in env.error


def test_dry_run_touches_nothing(home):
    ran = []

    class Probe(Pack):
        id = "probe"

        def deploy_steps(self, ctx):
            return [Step("a", "first", lambda c: ran.append("a")), Step("b", "second", lambda c: ran.append("b"))]

    env = run_pack(Probe(), "launch", "demo", {}, services={}, dry_run=True)
    assert env.mode == "dry-run" and len(env.items) == 2 and ran == [] and not (home / "packs").exists()


def test_steps_with_a_check_are_skipped_on_the_second_run(home):
    ran = []

    class Once(Pack):
        id = "once"

        def deploy_steps(self, ctx):
            return [Step("make", "make it", lambda c: ran.append(1) or {"made": True}, check=lambda c: bool(c.state.get("made")))]

    run_pack(Once(), "launch", "x", {}, services={})
    again = run_pack(Once(), "launch", "x", {}, services={})
    assert ran == [1] and again.summary["skipped"] == 1


def test_unexpected_exception_reports_class_only(home):
    class Boom(Pack):
        id = "boom"

        def deploy_steps(self, ctx):
            def go(c):
                raise RuntimeError("token=abc123")
            return [Step("go", "explode", go)]

    env = run_pack(Boom(), "launch", "x", {}, services={})
    assert "abc123" not in json.dumps(env.to_dict()) and "RuntimeError" in env.error


def test_registry_lists_the_current_packs_and_the_cli_refuses_unknown_ones(capsys, home):
    assert sorted(registry.all_packs()) == ["aml", "db-agent", "rag-ide", "rag-sec-tutorial"]
    assert main(["packs"]) == 0 and "rag-sec-tutorial" in capsys.readouterr().out
    assert main(["launch", "nope", "--name", "x"]) == 2


def test_removed_packs_exit_with_a_pointer_to_the_tutorial(capsys, home):
    for old in ("rag-lakebase", "rag-sec-filings", "rag-sec-filings-live", "rag-sec"):
        assert main(["launch", old, "--name", "x"]) == 2
        err = capsys.readouterr().err
        assert "was removed" in err and "rag-sec-tutorial" in err


# ---- the Databricks CLI wrapper --------------------------------------------------------------------------------------------------

def test_dbx_wrapper_redacts_tokens():
    assert "dapi" not in redact("auth failed dapi" + "a1" * 20)

    def run(cmd, **kw):
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "bad dapi" + "b2" * 20})()
    with pytest.raises(PackError) as e:
        Dbx(run=run, binary="/bin/databricks").text("apps", "get", "x")
    assert "dapi" not in str(e.value)


# ---- shared login handling (used by db-agent and the tutorial) -------------------------------------------------------------------

class StaleDefaultDbx:
    """The CLI's default profile is expired; 'GOOD' is valid for the same workspace."""
    def __init__(self, profiles):
        self.profile = None
        self._profiles = profiles

    def json(self, *a, body=None):
        if a[:2] == ("current-user", "me"):
            if self.profile != "GOOD":
                raise PackError("databricks current-user me failed: Error: refresh token is invalid")
            return {"userName": "learner@example.com"}
        return {}

    def profiles(self):
        return self._profiles


STALE = [{"name": "stale", "host": "https://w1", "valid": False, "default": True}]


def ctx_for(dbx, **params):
    return PackContext("x", "demo", params, services={"dbx": dbx})


def test_expired_default_profile_falls_back_to_the_valid_one_for_the_same_workspace(home):
    dbx = StaleDefaultDbx(STALE + [{"name": "GOOD", "host": "https://w1", "valid": True}])
    assert login(ctx_for(dbx))["userName"] == "learner@example.com" and dbx.profile == "GOOD"


def test_no_silent_fallback_to_a_different_workspace_and_errors_name_the_valid_profiles(home):
    dbx = StaleDefaultDbx(STALE + [{"name": "OTHER", "host": "https://w2", "valid": True}])
    with pytest.raises(PackError) as e:
        login(ctx_for(dbx))
    assert "OTHER" in str(e.value) and "--param profile=" in str(e.value) and dbx.profile is None


def test_an_explicit_profile_is_never_overridden(home):
    dbx = StaleDefaultDbx(STALE + [{"name": "GOOD", "host": "https://w1", "valid": True}])
    with pytest.raises(PackError):
        login(ctx_for(dbx, profile="stale"))
    assert dbx.profile is None


# ---- machine-readable catalog (the console seeds its lab list from this) ------------------------------------------------------------

def test_packs_json_describes_every_pack_with_platforms_and_params(capsys, home):
    assert main(["packs", "--json"]) == 0
    cat = {p["id"]: p for p in json.loads(capsys.readouterr().out)["packs"]}
    assert sorted(cat) == ["aml", "db-agent", "rag-ide", "rag-sec-tutorial"]
    assert cat["rag-ide"]["platforms"] == ["local", "ec2", "modal"] and cat["rag-ide"]["cost_tier"] == "metered"
    assert {"name": "hours", "default": 4, "help": "Hard cap per session, 1 to 24", "required": False} in cat["rag-ide"]["params"]
    assert all(p["title"] and p["summary"] and p["platforms"] and p["cost_tier"] for p in cat.values())


def test_packs_json_never_contains_a_credential(capsys, home, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 40)
    main(["packs", "--json"])
    assert "gsk_" not in capsys.readouterr().out
