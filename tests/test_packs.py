import json
import pytest

from labagent.cli import main
from labagent.packs import base, registry
from labagent.packs.base import Pack, PackError, Param, Step, run_pack
from labagent.packs.databricks.cli import Dbx, redact
from labagent.packs.rag_lakebase.pack import RagLakebase, corpus

DIM = 8


class FakeDbx:
    """Records calls and answers like the Databricks CLI would; state lives in dicts."""
    def __init__(self, projects=None, endpoints=("databricks-gte-large-en", "databricks-meta-llama-3-3-70b-instruct")):
        self.projects = dict(projects or {})
        self.apps = {}
        self.calls = []
        self.endpoints = endpoints

    def json(self, *a, body=None):
        self.calls.append(a)
        if a[:2] == ("current-user", "me"):
            return {"userName": "learner@example.com"}
        if a[0] == "serving-endpoints":
            return [{"name": n} for n in self.endpoints]
        if a[:2] == ("postgres", "list-projects"):
            return {"projects": [{"name": f"projects/{p}"} for p in self.projects]}
        if a[:2] == ("postgres", "create-project"):
            self.projects[a[2]] = True
            return {}
        if a[:2] == ("postgres", "delete-project"):
            self.projects.pop(a[2].split("/")[-1], None)
            return {}
        if a[:2] == ("postgres", "list-branches"):
            return {"branches": [{"name": f"{a[2]}/branches/main", "status": {"default": True}}]}
        if a[:2] == ("postgres", "list-endpoints"):
            return {"endpoints": [{"name": f"{a[2]}/endpoints/primary", "status": {"hosts": {"host": "ep.example.invalid"}}}]}
        if a[:2] == ("postgres", "generate-database-credential"):
            return {"token": "TOKEN-SECRET"}
        if a[:2] == ("postgres", "create-role"):
            return {}
        if a[:2] == ("apps", "create"):
            self.apps[a[2]] = {"url": f"https://{a[2]}.example.invalid", "service_principal_client_id": "sp-1234"}
            return {}
        if a[:2] == ("apps", "get"):
            return self.apps[a[2]]
        raise AssertionError(f"unexpected json call {a}")

    def text(self, *a, stdin=None):
        self.calls.append(a)
        if a[:2] == ("apps", "delete"):
            self.apps.pop(a[2], None)
        return ""

    def exists(self, *a):
        self.calls.append(a)
        if a[:2] == ("postgres", "get-project"):
            return a[2].split("/")[-1] in self.projects
        if a[:2] == ("apps", "get"):
            return a[2] in self.apps
        return False

    def api(self, method, path, body=None):
        self.calls.append((method, path))
        return {"data": [{"embedding": [0.1] * DIM} for _ in body["input"]]}


class FakePg:
    def __init__(self):
        self.rows = {}
        self.sql = []
        self.tokens = []

    def connect(self, host, user, token, dbname="databricks_postgres"):
        self.tokens.append(token)
        return FakeConn(self)


class FakeConn:
    def __init__(self, pg):
        self.pg = pg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.pg.sql.append(sql.split("(")[0].strip()[:60])
        if sql.startswith("INSERT INTO rag.chunks"):
            self.pg.rows[params[0]] = params
        if "SELECT count" in sql:
            return type("R", (), {"fetchone": lambda s: (len(self.pg.rows),)})()
        return None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    return tmp_path


def services(dbx=None, pg=None):
    pg = pg or FakePg()
    return {"dbx": dbx or FakeDbx(), "pg": pg.connect}, pg


def test_launch_end_to_end_and_idempotent(home):
    svc, pg = services()
    env = run_pack(RagLakebase(), "launch", "demo", {}, services=svc)
    assert env.status == "ok", env.error
    assert [i["state"] for i in env.items] == ["done"] * 7
    assert len(pg.rows) == len(corpus())
    out = env.summary["outputs"]
    assert out["app_url"].startswith("https://rag-demo") and out["documents"] == len(corpus())
    # re-run resumes: project and loaded rows are detected, nothing is re-embedded
    svc2, pg2 = services(dbx=svc["dbx"], pg=pg)
    again = run_pack(RagLakebase(), "launch", "demo", {}, services=svc2)
    states = {i["step"]: i["state"] for i in again.items}
    assert again.status == "ok" and states["project"] == "skipped" and states["load"] == "skipped"


def test_secrets_never_in_envelope_or_state(home):
    svc, pg = services()
    env = run_pack(RagLakebase(), "launch", "demo", {}, services=svc)
    blob = json.dumps(env.to_dict()) + (home / "packs/rag-lakebase/demo.json").read_text()
    assert "TOKEN-SECRET" not in blob
    assert pg.tokens and set(pg.tokens) == {"TOKEN-SECRET"}
    assert oct((home / "packs/rag-lakebase/demo.json").stat().st_mode & 0o777) == "0o600"


def test_free_edition_one_project_limit(home):
    svc, _ = services(dbx=FakeDbx(projects={"someone-elses": True}))
    env = run_pack(RagLakebase(), "launch", "demo", {}, services=svc)
    assert env.status == "failed" and "allows one" in env.error
    assert svc["dbx"].projects == {"someone-elses": True}   # nothing created


def test_missing_endpoints_gives_actionable_error(home):
    svc, _ = services(dbx=FakeDbx(endpoints=("custom-model",)))
    env = run_pack(RagLakebase(), "launch", "demo", {}, services=svc)
    assert env.status == "failed" and "--param embedding_endpoint" in env.error


def test_destroy_removes_only_what_it_created(home):
    svc, _ = services()
    run_pack(RagLakebase(), "launch", "demo", {}, services=svc)
    env = run_pack(RagLakebase(), "destroy", "demo", {}, services=svc)
    assert env.status == "ok"
    assert svc["dbx"].apps == {} and svc["dbx"].projects == {}
    assert not (home / "packs/rag-lakebase/demo.json").exists()


def test_destroy_keeps_a_preexisting_project(home):
    dbx = FakeDbx(projects={"rag-demo": True})
    svc, _ = services(dbx=dbx)
    run_pack(RagLakebase(), "launch", "demo", {}, services=svc)
    run_pack(RagLakebase(), "destroy", "demo", {}, services=svc)
    assert "rag-demo" in dbx.projects


def test_dry_run_touches_nothing(home):
    svc, pg = services()
    env = run_pack(RagLakebase(), "launch", "demo", {}, services=svc, dry_run=True)
    assert env.mode == "dry-run" and len(env.items) == 7 and not svc["dbx"].calls and not pg.sql
    assert not (home / "packs").exists()


def test_unknown_param_rejected(home):
    env = run_pack(RagLakebase(), "launch", "demo", {"nope": 1}, services=services()[0])
    assert env.status == "failed" and "unknown parameter" in env.error


def test_adding_a_pack_needs_no_framework_change(home):
    class Hello(Pack):
        id, title, summary = "hello", "Hello", "demo"
        params = [Param("who", "world")]

        def deploy_steps(self, ctx):
            return [Step("greet", "say hi", lambda c: {"greeting": f"hi {c.params['who']}"})]

    env = run_pack(Hello(), "launch", "x", {"who": "lab"}, services={})
    assert env.status == "ok" and base.load_state("hello", "x")["greeting"] == "hi lab"


def test_unexpected_exception_reports_class_only(home):
    class Boom(Pack):
        id = "boom"

        def deploy_steps(self, ctx):
            def go(c):
                raise RuntimeError("token=abc123")
            return [Step("go", "explode", go)]

    env = run_pack(Boom(), "launch", "x", {}, services={})
    assert "abc123" not in json.dumps(env.to_dict()) and "RuntimeError" in env.error


def test_registry_and_cli(capsys, home):
    assert "rag-lakebase" in registry.all_packs()
    assert main(["packs"]) == 0 and "rag-lakebase" in capsys.readouterr().out
    assert main(["launch", "nope", "--name", "x"]) == 2


def test_dbx_wrapper_redacts_tokens():
    assert "dapi" not in redact("auth failed dapi" + "a1" * 20)

    def run(cmd, **kw):
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "bad dapi" + "b2" * 20})()
    with pytest.raises(PackError) as e:
        Dbx(run=run, binary="/bin/databricks").text("apps", "get", "x")
    assert "dapi" not in str(e.value)


def test_corpus_is_clean():
    docs = corpus()
    assert len(docs) >= 20 and len({d["id"] for d in docs}) == len(docs)
    assert all("@" not in d["content"] for d in docs)
