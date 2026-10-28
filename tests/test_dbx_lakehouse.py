"""dbx-lakehouse: the pack with a fake warehouse and a fake Databricks CLI. No workspace, no network, no cost."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from labagent import datasets
from labagent.packs import registry
from labagent.packs.aml.licences import assert_redistributable
from labagent.packs.base import PackError, load_state, run_pack, state_path
from labagent.packs.dbx_lakehouse import notebook
from labagent.packs.dbx_lakehouse.domains import DOMAINS, landing_folder
from labagent.packs.dbx_lakehouse.pack import DatabricksLakehouse, GUIDES, SQL_DIR
from labagent.packs.dbx_lakehouse.sqlrun import Sql

PACK = DatabricksLakehouse()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path / "home"))
    return tmp_path


# ---- fakes ---------------------------------------------------------------------------------------------------------------------------

class FakeDbx:
    def __init__(self, warehouses=None):
        self.profile = None
        self.calls = []
        self.warehouses = [{"id": "wh1", "name": "Starter", "state": "RUNNING", "enable_serverless_compute": True}] if warehouses is None else warehouses

    def profiles(self):
        return [{"name": "DEFAULT", "host": "https://dbc-x.cloud.databricks.com", "valid": True, "default": True}]

    def json(self, *a, body=None):
        self.calls.append(a)
        if a[:2] == ("current-user", "me"):
            return {"userName": "teacher@example.com"}
        if a[:2] == ("warehouses", "list"):
            return self.warehouses
        if a[:2] == ("warehouses", "create"):
            return {"id": "wh-new"}
        if a[:2] == ("workspace", "get-status"):
            return {"object_id": 77}
        raise AssertionError(f"unexpected json call {a}")

    def text(self, *a, stdin=None):
        self.calls.append(a)
        return ""

    def cmds(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


class FakeSql:
    """Answers the few questions the pack asks, and records every statement."""

    def __init__(self, catalogs=(), schemas=(), counts=None, fail=(), create_catalog_fails=False, current="workspace", invariant="true"):
        self.catalogs, self.schemas, self.counts, self.fail = list(catalogs), set(schemas), counts or {}, list(fail)
        self.create_catalog_fails, self.current, self.invariant = create_catalog_fails, current, invariant
        self.log = []

    def run(self, statement, timeout=0, label=""):
        s = " ".join(statement.split())
        self.log.append(s)
        for needle, exc in self.fail:
            if needle in s:
                raise exc
        if s.startswith("SHOW CATALOGS"):
            return [[c] for c in self.catalogs if c == s.split("'")[1]]
        if s.startswith("SHOW SCHEMAS IN"):
            name = s.split("'")[1]
            return [[name]] if name in self.schemas else []
        if s.startswith("CREATE CATALOG") and self.create_catalog_fails:
            raise PackError("SQL failed: Metastore storage root URL does not exist")
        if s.startswith("SELECT current_catalog()"):
            return [[self.current]]
        m = re.match(r"SELECT count\(\*\) FROM (\S+)$", s)
        if m:
            return [[str(self.counts.get(m.group(1), 5))]]
        if s.startswith("SELECT ("):
            return [[self.invariant]]
        return []

    def scalar(self, statement):
        rows = self.run(statement)
        return rows[0][0] if rows and rows[0] else None

    def statements(self, prefix):
        return [s for s in self.log if s.startswith(prefix)]


def make_data(root: Path, domains=("retail", "healthcare")) -> Path:
    """A tiny unpacked snapshot per domain with the row counts the pack will check bronze against (5 per bronze table)."""
    if "retail" in domains:
        r = root / "retail"
        r.mkdir(parents=True, exist_ok=True)
        rows = {"retail_initial_2010-01.csv.gz": 2, "retail_initial_2010-02.csv.gz": 3, "retail_incoming_2010-03.csv.gz": 9}
        for n in (*rows, "NOTICE.txt"):
            (r / n).write_bytes(b"x")
        (r / "provenance.json").write_text(json.dumps({"sources": [{"id": "uci-online-retail-ii"}], "info": {"rows": rows}}))
    if "healthcare" in domains:
        h = root / "healthcare"
        h.mkdir(parents=True, exist_ok=True)
        tables = [t for t in DOMAINS["healthcare"].silver_gold["silver"]]
        rows = {f"healthcare_{t}.csv": 5 for t in tables}
        for n in (*rows, "NOTICE.txt"):
            (h / n).write_bytes(b"x")
        (h / "provenance.json").write_text(json.dumps({"sources": [{"id": "synthea-sample"}], "info": {"rows": rows}}))
    return root


def launch(home, sql=None, dbx=None, domains="retail,healthcare", **params):
    sql = sql or FakeSql()
    dbx = dbx or FakeDbx()
    data = make_data(home / "data")
    given = {"domains": domains, "data_dir": str(data), **params}
    env = run_pack(PACK, "launch", "demo", given, services={"dbx": dbx, "sql": sql})
    return env, sql, dbx


# ---- the shipped SQL itself ---------------------------------------------------------------------------------------------------------

def all_notebooks():
    return [(d, f) for d in DOMAINS.values() for f in (*d.notebooks, *([d.governance] if d.governance else []))]


def strip_strings_and_comments(sql: str) -> str:
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))


@pytest.mark.parametrize("dom,nb", all_notebooks())
def test_every_notebook_parses_into_single_statements_with_no_placeholder_left(dom, nb):
    text = notebook.substitute((SQL_DIR / dom.id / nb).read_text(), {"CATALOG": "lab", "CLINICAL_GROUP": "clinicians"})
    assert text.startswith(notebook.HEADER)
    cs = notebook.cells(text)
    assert cs, "a notebook with nothing to run"
    for c in cs:
        assert ";" not in strip_strings_and_comments(c.sql), f"two statements in one cell: {c.label}"
        assert "-- MAGIC" not in c.sql and "@optional" not in c.sql


@pytest.mark.parametrize("dom,nb", all_notebooks())
def test_layers_only_read_downward_and_only_write_their_own_schema(dom, nb):
    """Gold must never read bronze, silver must never read gold, and every statement writes only into the layer its notebook is for."""
    layer = {"01": "bronze", "02": "silver", "03": "gold", "04": "silver"}[nb[:2]]
    rank = {"bronze": 0, "silver": 1, "gold": 2}
    for c in notebook.cells((SQL_DIR / dom.id / nb).read_text()):
        body = strip_strings_and_comments(c.sql)
        used = re.findall(r"__CATALOG__\.(\w+)\.", c.sql)
        target = re.match(r"\s*(?:CREATE (?:OR REPLACE )?(?:TABLE|VIEW|FUNCTION)(?: IF NOT EXISTS)?|ALTER TABLE|COPY INTO)\s+__CATALOG__\.(\w+)\.", body)
        assert target, f"cannot find the target of: {c.label}"
        assert target.group(1) == dom.schema(layer), f"{nb} writes {target.group(1)}: {c.label}"
        for schema in used:
            assert schema.startswith(dom.id + "_"), f"{nb} touches another domain: {schema}"
            assert rank[schema.split("_", 1)[1]] <= rank[layer], f"{nb} reads upward from {schema}: {c.label}"
        if layer == "gold":
            assert not any(s.endswith("_bronze") for s in used), f"gold reads bronze: {c.label}"


def test_every_table_the_pack_checks_is_created_by_a_notebook_and_nothing_else_is_assumed():
    for dom in DOMAINS.values():
        created = set()
        for nb in dom.notebooks:
            for c in notebook.cells((SQL_DIR / dom.id / nb).read_text()):
                m = re.match(r"\s*CREATE (?:OR REPLACE )?TABLE(?: IF NOT EXISTS)? __CATALOG__\.(\w+)\.(\w+)", strip_strings_and_comments(c.sql))
                if m:
                    created.add(f"{m.group(1)}.{m.group(2)}")
        for layer, tables in dom.silver_gold.items():
            assert {f"{dom.schema(layer)}.{t}" for t in tables} <= created, f"{dom.id} {layer}: a checked table is never created"
        rows = {"retail": {"retail_initial_x.csv.gz": 1}, "healthcare": {f"healthcare_{t}.csv": 1 for t in dom.silver_gold["silver"]}}[dom.id]
        assert {f"{dom.schema('bronze')}.{t}" for t in dom.bronze_tables(rows)} <= created


def test_healthcare_governance_runs_last_and_masks_what_it_tags():
    gov = notebook.cells((SQL_DIR / "healthcare" / "04_governance.sql").read_text())
    text = "\n".join(c.sql for c in gov)
    assert "__CLINICAL_GROUP__" in text and "is_account_group_member" in text
    for col in ("ssn", "drivers", "passport", "address"):
        assert re.search(rf"ALTER COLUMN {col} SET MASK", text), f"{col} is not masked"
        assert re.search(rf"ALTER COLUMN {col} SET TAGS", text), f"{col} is not tagged"
    assert "SET ROW FILTER" in text
    assert DOMAINS["healthcare"].governance == "04_governance.sql" and DOMAINS["healthcare"].notebooks[-1] == "03_gold.sql"
    assert "bronze" not in DOMAINS["healthcare"].reader_layers and not DOMAINS["healthcare"].volume_readable


def test_notebook_cells_skip_markdown_and_comment_only_cells_and_flag_optional_ones():
    text = "\n".join([notebook.HEADER, "-- MAGIC %md", "-- MAGIC hello", "", "-- COMMAND ----------", "", "-- just a note", "",
                      "-- COMMAND ----------", "", "-- @optional", "ALTER TABLE a SET TAGS ('k' = 'v');", "", "-- COMMAND ----------", "", "SELECT 1", ""])
    cs = notebook.cells(text)
    assert [(c.sql, c.optional) for c in cs] == [("ALTER TABLE a SET TAGS ('k' = 'v')", True), ("SELECT 1", False)]
    with pytest.raises(ValueError, match="__NAME__"):
        notebook.substitute("SELECT __NAME__", {"CATALOG": "x"})


def test_guides_exist_and_use_only_known_placeholders():
    for f in [GUIDES / "START_HERE.md", *(GUIDES / f"{d}.md" for d in DOMAINS)]:
        notebook.substitute(f.read_text(), {"CATALOG": "lab", "CLINICAL_GROUP": "g"})


# ---- the pack ------------------------------------------------------------------------------------------------------------------------

def test_the_pack_is_registered_and_describes_itself():
    assert registry.resolve("dbx-lakehouse").id == registry.get("dbx-lakehouse").id == "dbx-lakehouse"
    assert registry.resolve("medallion").id == "dbx-lakehouse"
    d = PACK.describe()
    assert d["platforms"] == ["databricks"] and {"profile", "domains", "catalog", "readers", "clinical_group", "data_dir", "rebuild"} <= {p["name"] for p in d["params"]}


def test_launch_builds_both_domains_in_order_and_ends_with_notebooks(home):
    env, sql, dbx = launch(home)
    assert env.status == "ok", env.error
    steps = [i["step"] for i in env.items]
    assert steps == ["preflight", "catalog", "files-retail", "build-retail", "verify-retail", "files-healthcare", "build-healthcare",
                     "verify-healthcare", "govern-healthcare", "share", "notebooks"]
    assert all(i["state"] == "done" for i in env.items)
    # layers are built bronze, then silver, then gold, and governance comes after the checks
    order = [s for s in sql.log if s.startswith(("COPY INTO", "CREATE OR REPLACE TABLE", "CREATE OR REPLACE VIEW"))]
    first = {k: next(i for i, s in enumerate(order) if k in s) for k in ("retail_bronze", "retail_silver.invoice_lines", "retail_gold.daily_sales")}
    assert first["retail_bronze"] < first["retail_silver.invoice_lines"] < first["retail_gold.daily_sales"]
    assert sql.log.index(next(s for s in sql.log if "SET MASK" in s)) > sql.log.index(next(s for s in sql.log if "healthcare_gold.patient_360" in s))
    st = load_state("dbx-lakehouse", "demo")
    assert st["catalog"] == "lakehouse" and st["catalog_created"] is True and st["warehouse_id"] == "wh1"
    assert st["schemas"] == ["sandbox", *[f"{d}_{layer}" for d in ("retail", "healthcare") for layer in ("bronze", "silver", "gold")]]
    assert env.summary["outputs"]["url"].endswith("/browse/folders/77")


def test_raw_files_go_to_the_landing_volume_and_the_incoming_months_are_left_unloaded(home):
    env, sql, dbx = launch(home, domains="retail")
    assert env.status == "ok", env.error
    copied = {c[3]: c for c in dbx.cmds("fs", "cp")}
    base = "dbfs:/Volumes/lakehouse/retail_bronze/landing"
    assert f"{base}/initial/retail_initial_2010-01.csv.gz" in copied and f"{base}/incoming/retail_incoming_2010-03.csv.gz" in copied
    assert f"{base}/NOTICE.txt" in copied                                      # the CC BY notice travels with the data
    assert not any("provenance" in k for k in copied)
    copy_into = sql.statements("COPY INTO")
    assert len(copy_into) == 1 and "/landing/initial'" in copy_into[0] and "incoming" not in copy_into[0]
    assert landing_folder("retail_initial_2010-01.csv.gz") == "initial" and landing_folder("healthcare_patients.csv") == ""


def test_learners_get_silver_and_gold_but_never_the_raw_healthcare_zone(home):
    env, sql, _ = launch(home)
    grants = sql.statements("GRANT")
    assert any("SCHEMA lakehouse.healthcare_silver TO `account users`" in g for g in grants)
    assert any("SCHEMA lakehouse.healthcare_gold TO `account users`" in g for g in grants)
    assert not any("healthcare_bronze" in g for g in grants)
    assert any("SCHEMA lakehouse.retail_bronze TO" in g for g in grants) and any("READ VOLUME ON VOLUME lakehouse.retail_bronze.landing" in g for g in grants)
    assert any("ON SCHEMA lakehouse.sandbox TO `account users`" in g and "CREATE TABLE" in g for g in grants)
    assert any("GRANT USE CATALOG ON CATALOG lakehouse" in g for g in grants)
    env2, sql2, _ = launch(home / "other", readers="")
    assert env2.status == "ok" and not sql2.statements("GRANT")


def test_when_the_catalog_cannot_be_created_the_current_catalog_is_used_and_not_granted(home):
    env, sql, _ = launch(home, sql=FakeSql(create_catalog_fails=True, current="workspace"), domains="retail")
    assert env.status == "ok", env.error
    st = load_state("dbx-lakehouse", "demo")
    assert st["catalog"] == "workspace" and st["catalog_created"] is False
    assert any("workspace.retail_gold.daily_sales" in s for s in sql.log)
    assert not any("USE CATALOG" in g for g in sql.statements("GRANT"))


def test_no_unity_catalog_at_all_is_a_clear_failure(home):
    env, _, _ = launch(home, sql=FakeSql(create_catalog_fails=True, current="hive_metastore"), domains="retail")
    assert not env.status == "ok" and "Unity Catalog" in env.error


def test_an_existing_schema_the_pack_did_not_create_is_never_touched(home):
    env, sql, _ = launch(home, sql=FakeSql(catalogs=["lakehouse"], schemas={"retail_bronze"}), domains="retail")
    assert not env.status == "ok" and "already exists and was not created by this pack" in env.error
    assert not any(s.startswith(("DROP", "CREATE OR REPLACE")) for s in sql.log)


def test_an_optional_statement_may_fail_but_a_required_one_stops_the_build(home):
    tag_failure = [("SET TAGS", PackError("SQL failed: tags are not supported here"))]
    env, sql, _ = launch(home, sql=FakeSql(fail=tag_failure), domains="healthcare")
    assert env.status == "ok", env.error
    assert load_state("dbx-lakehouse", "demo")["domains"]["healthcare"]["governance_skipped"]
    assert env.summary["outputs"]["domains"]["healthcare"]["optional_skipped"] >= 5
    env2, _, _ = launch(home / "b", sql=FakeSql(fail=[("retail_silver.invoice_lines_rejected", PackError("SQL failed: boom"))]), domains="retail")
    assert not env2.status == "ok" and env2.items[-1]["step"] == "build-retail" and "boom" in env2.error


def test_the_checks_fail_when_a_bronze_table_has_the_wrong_number_of_rows(home):
    env, _, _ = launch(home, sql=FakeSql(counts={"lakehouse.retail_bronze.invoice_lines_raw": 4}), domains="retail")
    assert not env.status == "ok" and "bronze.invoice_lines_raw has 4 rows, expected 5" in env.error


def test_the_checks_fail_when_an_invariant_breaks_or_a_gold_table_is_empty(home):
    env, _, _ = launch(home, sql=FakeSql(invariant="false"), domains="retail")
    assert not env.status == "ok" and "every bronze line is either in silver or in the rejected table" in env.error
    env2, _, _ = launch(home / "b", sql=FakeSql(counts={"lakehouse.retail_gold.customer_rfm": 0}), domains="retail")
    assert not env2.status == "ok" and "gold.customer_rfm is empty" in env2.error


def test_a_second_launch_skips_what_is_finished_and_rebuild_runs_it_again(home):
    sql, dbx = FakeSql(), FakeDbx()
    env, _, _ = launch(home, sql=sql, dbx=dbx, domains="retail")
    assert env.status == "ok"
    data = home / "data"
    again = run_pack(PACK, "launch", "demo", {"domains": "retail", "data_dir": str(data)}, services={"dbx": dbx, "sql": sql})
    states = {i["step"]: i["state"] for i in again.items}
    assert states["catalog"] == states["files-retail"] == states["build-retail"] == states["verify-retail"] == states["share"] == "skipped"
    assert states["preflight"] == states["notebooks"] == "done"
    sql.log.clear()
    rebuilt = run_pack(PACK, "launch", "demo", {"domains": "retail", "data_dir": str(data), "rebuild": "yes"}, services={"dbx": dbx, "sql": sql})
    assert rebuilt.status == "ok" and sql.statements("DROP TABLE IF EXISTS lakehouse.retail_bronze.invoice_lines_raw")
    assert sql.log.index("DROP TABLE IF EXISTS lakehouse.retail_bronze.invoice_lines_raw") < sql.log.index(sql.statements("COPY INTO")[0])


def test_bad_parameters_are_refused_before_anything_runs(home):
    for bad in ({"domains": "retail,finance"}, {"domains": ""}, {"catalog": "Bad-Name"}, {"catalog": "x; DROP"}, {"readers": "a`b"},
                {"readers": "x' OR '1'='1"}, {"clinical_group": "g'; DROP"}):
        sql = FakeSql()
        env, _, _ = launch(home / str(abs(hash(str(bad)))), sql=sql, **bad)
        assert not env.status == "ok" and env.items[0]["step"] == "preflight" and env.items[0]["state"] == "failed", bad
        assert sql.log == [], bad


def test_no_data_means_a_clear_error_and_a_download_is_checked_against_its_pin(home, monkeypatch):
    env = run_pack(PACK, "launch", "demo", {"domains": "retail", "data_dir": str(home / "nope")}, services={"dbx": FakeDbx(), "sql": FakeSql()})
    assert not env.status == "ok" and "provenance.json" in env.error
    monkeypatch.setattr(datasets, "registry", lambda: {"lakehouse-retail-2026.10": {"kind": "tables", "url": "https://x.example/a.zip", "sha256": "0" * 64}})
    monkeypatch.setenv("HOME", str(home))
    env = run_pack(PACK, "launch", "demo2", {"domains": "retail"},
                   services={"dbx": FakeDbx(), "sql": FakeSql(), "download": lambda u, d: d.write_bytes(b"not the pinned file")})
    assert not env.status == "ok" and "matching checksum" in env.error


def test_a_real_snapshot_round_trips_through_the_pinned_download(home, monkeypatch):
    src = home / "src"
    make_data(src, domains=("retail",))
    out = datasets.build_table_snapshot(src / "retail", "lakehouse-retail", "test", home / "out")
    entry = {"kind": "tables", "url": "https://x.example/r.zip", "sha256": out["sha256"], "bytes": out["bytes"]}
    monkeypatch.setattr(datasets, "registry", lambda: {"lakehouse-retail-2026.10": entry})
    cache = home / "cache"
    monkeypatch.setenv("LABAGENT_HOME", str(cache))
    zpath = home / "out" / out["file"]
    dbx = FakeDbx()
    env = run_pack(PACK, "launch", "demo", {"domains": "retail"},
                   services={"dbx": dbx, "sql": FakeSql(), "download": lambda u, d: d.write_bytes(zpath.read_bytes())})
    assert env.status == "ok", env.error
    assert any(c[3].endswith("/initial/retail_initial_2010-02.csv.gz") for c in dbx.cmds("fs", "cp"))


def test_warehouse_choice_prefers_serverless_and_running_and_creates_one_only_when_there_is_none(home):
    many = [{"id": "a", "state": "STOPPED", "enable_serverless_compute": False}, {"id": "b", "state": "STOPPED", "enable_serverless_compute": True},
            {"id": "c", "state": "RUNNING", "enable_serverless_compute": True}, {"id": "d", "state": "DELETED"}]
    env, _, dbx = launch(home, dbx=FakeDbx(many), domains="retail")
    assert load_state("dbx-lakehouse", "demo")["warehouse_id"] == "c" and not dbx.cmds("warehouses", "create")
    env2, _, dbx2 = launch(home / "b", dbx=FakeDbx([]), domains="retail")
    assert load_state("dbx-lakehouse", "demo")["warehouse_id"] == "wh-new" and load_state("dbx-lakehouse", "demo")["warehouse_created"] is True
    env3, _, _ = launch(home / "c", dbx=FakeDbx(many), domains="retail", warehouse_id="zzz")
    assert not env3.status == "ok" and "zzz was not found" in env3.error


def test_destroy_drops_only_what_was_created_and_forgets_it(home):
    sql, dbx = FakeSql(), FakeDbx([])
    env, _, _ = launch(home, sql=sql, dbx=dbx, domains="retail")
    assert env.status == "ok"
    sql.log.clear()
    out = run_pack(PACK, "destroy", "demo", {"domains": "retail"}, services={"dbx": dbx, "sql": sql})
    assert out.status == "ok", out.error
    drops = sql.statements("DROP")
    assert drops == [f"DROP SCHEMA IF EXISTS lakehouse.{s} CASCADE" for s in ("retail_gold", "retail_silver", "retail_bronze", "sandbox")] + ["DROP CATALOG IF EXISTS lakehouse CASCADE"]
    assert dbx.cmds("workspace", "delete") and dbx.cmds("warehouses", "delete")
    assert not state_path("dbx-lakehouse", "demo").exists()


def test_destroy_leaves_a_catalog_and_a_warehouse_it_did_not_create(home):
    sql, dbx = FakeSql(create_catalog_fails=True), FakeDbx()
    launch(home, sql=sql, dbx=dbx, domains="retail")
    sql.log.clear()
    out = run_pack(PACK, "destroy", "demo", {}, services={"dbx": dbx, "sql": sql})
    assert out.status == "ok", out.error
    assert not sql.statements("DROP CATALOG") and not dbx.cmds("warehouses", "delete")
    assert all(s.startswith("DROP SCHEMA IF EXISTS workspace.") for s in sql.statements("DROP"))


def test_destroy_with_nothing_recorded_does_nothing(home):
    sql, dbx = FakeSql(), FakeDbx()
    out = run_pack(PACK, "destroy", "never-launched", {}, services={"dbx": dbx, "sql": sql})
    assert out.status == "ok" and sql.log == [] and not dbx.cmds("workspace", "delete")


def test_dry_run_lists_the_steps_and_touches_nothing(home):
    sql, dbx = FakeSql(), FakeDbx()
    env = run_pack(PACK, "launch", "demo", {"domains": "retail,healthcare"}, services={"dbx": dbx, "sql": sql}, dry_run=True)
    assert env.status == "ok" and len(env.items) == 11 and all(i["state"] == "planned" for i in env.items) and sql.log == [] and dbx.calls == []


# ---- the SQL runner ------------------------------------------------------------------------------------------------------------------

class RestDbx:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def json(self, *a, body=None):
        self.calls.append((a, body))
        return self.script.pop(0)


def test_sql_runner_polls_until_the_statement_finishes_and_returns_rows():
    dbx = RestDbx([{"statement_id": "s1", "status": {"state": "PENDING"}}, {"statement_id": "s1", "status": {"state": "RUNNING"}},
                   {"statement_id": "s1", "status": {"state": "SUCCEEDED"}, "result": {"data_array": [["42"]]}}])
    naps = []
    sql = Sql(dbx, "wh1", sleep=naps.append)
    assert sql.scalar("SELECT 42") == "42" and len(naps) == 2
    assert dbx.calls[0][0] == ("api", "post", "/api/2.0/sql/statements") and dbx.calls[0][1]["warehouse_id"] == "wh1"
    assert dbx.calls[1][0] == ("api", "get", "/api/2.0/sql/statements/s1")


def test_sql_runner_reports_the_reason_for_a_failure_and_gives_up_on_a_stuck_statement():
    failed = RestDbx([{"statement_id": "s", "status": {"state": "FAILED", "error": {"message": "TABLE_OR_VIEW_NOT_FOUND:\n  x   missing"}}}])
    with pytest.raises(PackError, match=r"SQL failed \(build\): TABLE_OR_VIEW_NOT_FOUND: x missing"):
        Sql(failed, "wh").run("SELECT * FROM x", label="build")
    clock = iter([0, 1, 5000, 5001, 5002])
    stuck = RestDbx([{"statement_id": "s", "status": {"state": "RUNNING"}}] * 5)
    with pytest.raises(PackError, match="timed out"):
        Sql(stuck, "wh", sleep=lambda s: None, clock=lambda: next(clock)).run("SELECT 1", timeout=100)


# ---- the data build and its licences -------------------------------------------------------------------------------------------------

def test_both_sources_may_be_redistributed_and_the_pins_match_the_domains():
    assert_redistributable(["uci-online-retail-ii", "synthea-sample"])
    reg = datasets.registry()
    for dom in DOMAINS.values():
        e = reg[dom.dataset]
        assert e["kind"] == "tables" and len(e["sha256"]) == 64 and e["url"].endswith(f"{dom.dataset}.tables.zip")


def test_the_data_builder_keeps_rows_unchanged_and_refuses_a_missing_table(tmp_path):
    spec = importlib.util.spec_from_file_location("build_lakehouse_data", Path(__file__).parents[1] / "scripts" / "build_lakehouse_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import zipfile
    z = tmp_path / "s.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for t in mod.HEALTH_TABLES:
            zf.writestr(f"csv/{t}.csv", "Id,NAME\n1,a\n2,\"b\nc\"\n")
    stage = tmp_path / "stage"
    stage.mkdir()
    counts = mod.build_healthcare(z, stage)
    assert counts["healthcare_patients.csv"] == 2                           # a quoted newline is one record, not two
    assert (stage / "healthcare_patients.csv").read_text() == "Id,NAME\n1,a\n2,\"b\nc\"\n"
    with zipfile.ZipFile(tmp_path / "bad.zip", "w") as zf:
        zf.writestr("csv/patients.csv", "Id\n")
    with pytest.raises(SystemExit, match="encounters.csv"):
        mod.build_healthcare(tmp_path / "bad.zip", tmp_path)
