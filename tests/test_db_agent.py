import json
from pathlib import Path

import pytest

from labagent.packs import gitsrc
from labagent.packs.base import PackError, run_pack
from labagent.packs.db_agent import pack as dbp
from labagent.packs.db_agent.pack import DbAgent, PINNED, REPO

TOKEN = "dapi" + "a1b2c3d4" * 5          # looks like a real token; must never appear in output or state


class FakeDbx:
    def __init__(self, warehouses=None, schemas=("bakehouse", "tpch"), endpoints=("databricks-gte-large-en", "databricks-gpt-oss-120b"),
                 token_create_fails=False, put_fails=False):
        self.profile = None
        self.calls, self.stdin = [], {}
        self.scopes, self.secrets, self.apps, self.tokens = set(), {}, {}, set()
        self.warehouses = [{"id": "wh1", "name": "Starter"}] if warehouses is None else warehouses
        self.schemas, self.endpoints = schemas, endpoints
        self.token_create_fails, self.put_fails = token_create_fails, put_fails
        self.imported = {}

    def profiles(self):
        return [{"name": "DEFAULT", "host": "https://dbc-x.cloud.databricks.com", "valid": True, "default": True}]

    def json(self, *a, body=None):
        self.calls.append(a)
        k = a[:2]
        if k == ("current-user", "me"):
            return {"userName": "learner@example.com"}
        if k == ("warehouses", "list"):
            return self.warehouses
        if k == ("schemas", "list"):
            return [{"name": s} for s in self.schemas]
        if a[0] == "serving-endpoints":
            return [{"name": n} for n in self.endpoints]
        if k == ("secrets", "list-scopes"):
            return {"scopes": [{"name": s} for s in self.scopes]}
        if k == ("secrets", "list-secrets"):
            return {"secrets": [{"key": key} for (sc, key) in self.secrets if sc == a[2]]}
        if k == ("tokens", "create"):
            if self.token_create_fails:
                raise PackError("databricks tokens create failed: PERMISSION_DENIED")
            self.tokens.add("tok-1")
            return {"token_value": TOKEN, "token_info": {"token_id": "tok-1"}}
        if k == ("apps", "create"):
            self.apps[body["name"]] = {"url": f"https://{body['name']}.example.invalid", "state": "RUNNING", "desc": body["description"], "res": body["resources"]}
            return {}
        if k == ("apps", "update"):
            self.apps[a[2]].update(desc=body["description"], res=body["resources"])
            return {}
        if k == ("apps", "get"):
            # real responses carry several unrelated "state" fields; the app's own status is app_status.state
            return {"active_deployment": {"status": {"state": "SUCCEEDED"}}, "compute_status": {"state": "ACTIVE"},
                    "url": self.apps[a[2]]["url"], "app_status": {"state": self.apps[a[2]]["state"]}}
        if k == ("auth", "token"):
            return {"access_token": "oauth-bearer"}
        raise AssertionError(f"unexpected json call {a}")

    def text(self, *a, stdin=None):
        self.calls.append(a)
        k = a[:2]
        if k == ("secrets", "create-scope"):
            self.scopes.add(a[2])
        elif k == ("secrets", "put-secret"):
            if self.put_fails:
                raise PackError("put-secret failed")
            self.secrets[(a[2], a[3])] = True
            self.stdin[a[3]] = stdin
        elif k == ("secrets", "delete-scope"):
            self.scopes.discard(a[2])
        elif k == ("tokens", "delete"):
            self.tokens.discard(a[2])
        elif k == ("workspace", "import-dir"):
            self.imported = {"src": a[2], "yaml": (Path(a[2]) / "app.yaml").read_text(), "files": sorted(p.name for p in Path(a[2]).iterdir())}
        elif k == ("apps", "delete"):
            self.apps.pop(a[2], None)
        return ""

    def exists(self, *a):
        self.calls.append(a)
        return a[:2] == ("apps", "get") and a[2] in self.apps


class Svc:
    def __init__(self, dbx, answer=None):
        self.asked = []
        answer = answer or (200, {"status": "complete", "sql": "SELECT count(*) FROM sales_customers", "rows": [[300]]})
        self.answers = answer if isinstance(answer, list) else [answer]

        def http(url, bearer, body=None, timeout=240):
            self.asked.append((url, bearer, body))
            return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        self.map = {"dbx": dbx, "http": http, "sleep": lambda s: None}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    monkeypatch.delenv("LABAGENT_DATABRICKS_TOKEN", raising=False)

    def fake_clone(url, sha, dest, run=None):
        (dest / "app" / "web").mkdir(parents=True, exist_ok=True)
        (dest / "app" / "server.js").write_text("// app")
        (dest / "app" / "app.yaml").write_text("# repo yaml with someone else's workspace host")
        (dest / "app" / ".env").write_text("SECRET=1")
        (dest / "app" / "node_modules").mkdir(exist_ok=True)
        return dest
    monkeypatch.setattr(dbp, "clone_pinned", fake_clone)
    return tmp_path


def launch(dbx, params=None, answer=None):
    svc = Svc(dbx, answer)
    return run_pack(DbAgent(), "launch", "demo", params or {}, services=svc.map), svc


def test_launch_deploys_pinned_source_with_a_rendered_app_yaml_and_smoke_tests_it(home):
    dbx = FakeDbx()
    env, svc = launch(dbx)
    assert env.status == "ok", env.error
    assert [i["state"] for i in env.items] == ["done"] * 5
    y = dbx.imported["yaml"]
    assert "https://dbc-x.cloud.databricks.com" in y and "/sql/1.0/warehouses/wh1" in y and 'value: "bakehouse"' in y
    assert "databricks-gpt-oss-120b" in y and "databricks-gte-large-en" in y and "someone else" not in y     # our yaml replaces the repo's
    assert ".env" not in dbx.imported["files"] and "node_modules" not in dbx.imported["files"]               # secrets and junk are not uploaded
    out = env.summary["outputs"]
    assert out["source"] == f"{REPO}@{PINNED[:12]}" and out["dataset"] == "samples.bakehouse" and out["smoke_status"] == "complete"
    assert svc.asked[0][0].endswith("/api/ask") and svc.asked[0][1] == "oauth-bearer"


def test_the_repo_is_credited_in_the_app_description_for_the_backlink(home):
    dbx = FakeDbx()
    launch(dbx)
    assert REPO in dbx.apps["db-agent-demo"]["desc"]


def test_the_token_is_only_ever_stored_as_a_secret(home):
    dbx = FakeDbx()
    env, _ = launch(dbx)
    state = (home / "packs/db-agent/demo.json").read_text()
    assert TOKEN not in json.dumps(env.to_dict()) + state + dbx.imported["yaml"] + repr([c for c in dbx.calls])
    assert dbx.stdin["token"] == TOKEN                                  # delivered via stdin, not on a command line
    assert dbx.apps["db-agent-demo"]["res"][0]["secret"] == {"scope": "labagent-db-agent-demo", "key": "token", "permission": "READ"}


def test_rerun_resumes_without_a_second_token(home):
    dbx = FakeDbx()
    launch(dbx)
    env, _ = launch(dbx)
    states = {i["step"]: i["state"] for i in env.items}
    assert states["fetch"] == "skipped" and states["token"] == "skipped" and env.status == "ok"
    assert sum(1 for c in dbx.calls if c[:2] == ("tokens", "create")) == 1


def test_orphaned_token_is_revoked_if_storing_it_fails(home):
    dbx = FakeDbx(put_fails=True)
    env, _ = launch(dbx)
    assert env.status == "failed" and dbx.tokens == set()


def test_token_creation_refused_gives_a_manual_route(home):
    env, _ = launch(FakeDbx(token_create_fails=True))
    assert env.status == "failed" and "LABAGENT_DATABRICKS_TOKEN" in env.error


def test_an_existing_token_from_the_environment_is_used_and_never_revoked(home, monkeypatch):
    monkeypatch.setenv("LABAGENT_DATABRICKS_TOKEN", "dapi" + "9f" * 20)
    dbx = FakeDbx()
    env, svc = launch(dbx)
    assert env.status == "ok" and not any(c[:2] == ("tokens", "create") for c in dbx.calls)
    run_pack(DbAgent(), "destroy", "demo", {}, services=svc.map)
    assert not any(c[:2] == ("tokens", "delete") for c in dbx.calls)


def test_preflight_failures_are_actionable_and_create_nothing(home):
    for dbx, needle in ((FakeDbx(warehouses=[]), "no SQL warehouse"), (FakeDbx(schemas=("tpch",)), "bakehouse"),
                        (FakeDbx(endpoints=("x",)), "serving endpoint")):
        env, _ = launch(dbx)
        assert env.status == "failed" and needle in env.error and not dbx.apps and not dbx.tokens and not dbx.scopes


def test_an_unpinned_ref_or_non_https_repo_is_refused(home, monkeypatch):
    monkeypatch.undo()                      # use the real clone_pinned for validation
    monkeypatch.setenv("LABAGENT_HOME", str(home))
    for params, needle in (({"ref": "main"}, "40-character"), ({"repo": "ssh://github.com/a/b"}, "https")):
        env, _ = launch(FakeDbx(), params)
        assert env.status == "failed" and needle in env.error


def test_failed_smoke_test_is_reported_not_hidden(home):
    env, _ = launch(FakeDbx(), answer=(500, {"error": "HTTP 500"}))
    assert env.status == "failed" and "did not answer a test question" in env.error


def test_smoke_test_retries_while_the_warehouse_starts(home):
    env, svc = launch(FakeDbx(), answer=[(500, {"error": "warming"}), (200, {"status": "complete", "sql": "SELECT 1", "rows": [[1]]})])
    assert env.status == "ok" and len(svc.asked) == 2


def test_destroy_removes_app_code_token_and_the_scope_it_created(home):
    dbx = FakeDbx()
    _, svc = launch(dbx)
    env = run_pack(DbAgent(), "destroy", "demo", {}, services=svc.map)
    assert env.status == "ok" and not dbx.apps and not dbx.tokens and "labagent-db-agent-demo" not in dbx.scopes
    assert not (home / "packs/db-agent/demo.json").exists()


def test_destroy_leaves_a_scope_it_did_not_create(home):
    dbx = FakeDbx()
    dbx.scopes.add("labagent-db-agent-demo")
    _, svc = launch(dbx)
    run_pack(DbAgent(), "destroy", "demo", {}, services=svc.map)
    assert "labagent-db-agent-demo" in dbx.scopes and not dbx.tokens


# -- pinned git fetch --------------------------------------------------------------
class GitRun:
    def __init__(self, head):
        self.head, self.cmds = head, []

    def __call__(self, cmd, cwd=None, **kw):
        self.cmds.append(cmd[1:])
        out = self.head if cmd[1] == "rev-parse" else ""
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()


def test_clone_pinned_fetches_the_exact_sha_and_verifies_head(tmp_path):
    run = GitRun(PINNED)
    gitsrc.clone_pinned(REPO, PINNED, tmp_path / "c", run=run)
    assert ["fetch", "-q", "--depth", "1", REPO, PINNED] in run.cmds and ["checkout", "-q", "FETCH_HEAD"] in run.cmds


def test_clone_pinned_refuses_a_commit_that_does_not_match(tmp_path):
    with pytest.raises(PackError, match="does not match"):
        gitsrc.clone_pinned(REPO, PINNED, tmp_path / "c", run=GitRun("0" * 40))
    assert not (tmp_path / "c").exists()


def test_clone_pinned_validates_inputs(tmp_path):
    for url, sha in (("http://github.com/a/b", PINNED), ("https://github.com/a/b", "main"), ("https://github.com/a/b", PINNED[:12])):
        with pytest.raises(PackError):
            gitsrc.clone_pinned(url, sha, tmp_path / "x", run=GitRun(PINNED))


def test_the_pin_is_a_real_full_sha():
    assert gitsrc.SHA.match(PINNED)


def test_app_status_is_read_from_the_exact_field(home):
    """Regression (live): a loose search found a deployment's 'SUCCEEDED' instead of the app's 'RUNNING' and waited 10 minutes."""
    dbx = FakeDbx()
    sleeps = []
    svc = Svc(dbx)
    svc.map["sleep"] = sleeps.append
    env = run_pack(DbAgent(), "launch", "demo", {}, services=svc.map)
    assert env.status == "ok" and sleeps == []                   # no polling delay when the app is already RUNNING
