import ast
import json
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from labagent import datasets
from labagent.packs import registry
from labagent.packs.base import PackError, load_state, run_pack
from labagent.packs.rag import tutorial_pack as tp
from labagent.packs.rag.sources import get as get_source

NB = tp.NOTEBOOKS
NAMES = ["00_setup", "01_data", "02_embeddings", "03_pgvector", "04_search", "05_answer_and_eval", "06_deploy_app", "99_cleanup", "_common"]


def cells(name):
    """(kind, text) per cell. kind is 'md', 'magic' or 'code'. MAGIC prefixes are removed."""
    out = []
    for raw in (NB / f"{name}.py").read_text().split("\n# COMMAND ----------\n"):
        lines = raw.strip("\n").splitlines()
        if lines and lines[0] == "# Databricks notebook source":
            lines = lines[1:]
        body = [re.sub(r"^# MAGIC ?", "", l) for l in lines] if any(l.startswith("# MAGIC") for l in lines) else lines
        text = "\n".join(body).strip("\n")
        kind = "md" if text.startswith("%md") else "magic" if text.startswith("%") else "code"
        if text:
            out.append((kind, text))
    return out


def test_every_notebook_is_in_databricks_source_format():
    assert sorted(p.stem for p in NB.glob("*.py")) == sorted(NAMES)
    for n in NAMES:
        text = (NB / f"{n}.py").read_text()
        assert text.startswith("# Databricks notebook source\n"), n
        for kind, code in cells(n):
            if kind == "code":
                ast.parse(code)                        # every code cell is valid Python


def test_notebooks_follow_the_writing_rules():
    for n in NAMES:
        text = (NB / f"{n}.py").read_text()
        assert not re.search("[—–]", text), f"{n} has an em or en dash"
        assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", text), f"{n} has an emoji"
        assert "@gmail" not in text and "@example" not in text, n


def test_numbered_notebooks_end_with_a_check_and_say_what_is_next():
    for n in NAMES[:-1]:
        text = (NB / f"{n}.py").read_text()
        assert "check(" in text, f"{n} has no check cell"
        if n != "99_cleanup":
            assert "**Next:**" in text and "What to notice" in text or n == "00_setup", n


def test_psycopg_is_installed_before_it_is_needed_and_python_restarts():
    for n in ("03_pgvector", "04_search", "05_answer_and_eval", "06_deploy_app", "99_cleanup"):
        kinds = [(k, t) for k, t in cells(n)]
        pip = next(i for i, (k, t) in enumerate(kinds) if t.startswith("%pip install"))
        restart = next(i for i, (k, t) in enumerate(kinds) if "restartPython()" in t)
        run = next(i for i, (k, t) in enumerate(kinds) if t.startswith("%run ./_common"))
        assert pip < restart < run, n
    for n in ("00_setup", "01_data", "02_embeddings"):
        assert not any(t.startswith("%pip") for _, t in cells(n)), f"{n} should not need a restart"


def test_common_is_filled_in_with_the_pinned_dataset_and_no_placeholder_survives():
    entry = datasets.registry()[tp.DATASET]
    text = tp.notebook_text("_common.py", {"SNAPSHOT_URL": entry["url"], "SNAPSHOT_SHA256": entry["sha256"], "WORKSPACE_FOLDER": "/Workspace/Users/x/y"})
    assert entry["url"] in text and entry["sha256"] in text and '"/Workspace/Users/x/y"' in text
    assert not re.search(r"__[A-Z_]{4,}__", text)
    assert "__SNAPSHOT_URL__" in (NB / "_common.py").read_text()          # the source file keeps the placeholders


def test_the_notebook_search_is_the_same_algorithm_as_the_deployed_apps():
    src = (NB / "_common.py").read_text()
    app = (tp.APP / "retrieval.py").read_text()
    for fragment in ("websearch_to_tsquery('english', %s)", "ts_rank_cd(tsv, q)", "RRF_K = 60", "ORDER BY embedding <=> %s::vector", "meta @> %s::jsonb",
                     "1.0 / (RRF_K + rank)"):
        assert fragment in src and fragment in app, fragment

    class Conn:                                              # same canned rows for both implementations
        def __init__(self): self.n = 0
        def execute(self, sql, params=()):
            self.n += 1
            rows = [("a", "A", "text a", {"ticker": "AAPL"}), ("b", "B", "text b", {"ticker": "WMT"}), ("c", "C", "text c", {"ticker": "PFE"})]
            return type("R", (), {"fetchall": lambda s, rows=rows if self.n % 2 else rows[::-1]: rows})()

    retrieval = type("M", (), {})()
    ns = {}
    exec(compile((tp.APP / "retrieval.py").read_text(), "retrieval", "exec"), ns)
    mine = {"json": json, "SCHEMA": "tutorial", "TOP_K": 4, "vec": ns["vec_literal"]}
    section = src[src.index("RRF_K = 60"):src.index("def is_hit")]
    exec(section, mine)
    a = [(r["id"], r["score"]) for r in mine["hybrid_search"](Conn(), [0.1, 0.2], "q", 3)]
    b = [(r["id"], r["score"]) for r in ns["search"](Conn(), "tutorial", [0.1, 0.2], "q", 3)]
    assert a == b and len(a) == 3


def test_notebook_05_evaluation_cases_match_the_packs_own_questions():
    tree = ast.parse("\n\n".join(t for k, t in cells("05_answer_and_eval") if k == "code"))
    cases = next(ast.literal_eval(n.value) for n in ast.walk(tree) if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "cases")
    theirs = [(q["question"], q["expect"]) for q in get_source("sec-10k-snapshot").eval_set() if q["expect"]["ticker"] != "XOM"]
    assert cases == theirs and len(cases) == 10
    assert "GATE = 0.6" in (NB / "05_answer_and_eval.py").read_text()


def test_the_app_notebook_fills_every_placeholder_the_app_yaml_has():
    placeholders = set(re.findall(r"__([A-Z_]+)__", (tp.APP / "app.yaml").read_text()))
    nb = (NB / "06_deploy_app.py").read_text()
    settings = set(re.findall(r'"([A-Z_]+)":', nb[nb.index("settings = {"):nb.index("yaml_path")]))
    assert placeholders == settings, (placeholders, settings)
    assert '"CAN_QUERY"' in nb and "SERVICE_PRINCIPAL" in nb and "LAKEBASE_OAUTH_V1" in nb and "GRANT SELECT" in nb


def test_staged_apple_demo_is_in_notebook_03_in_order():
    code = [t for k, t in cells("03_pgvector") if k == "code"]
    idx = lambda needle, start=0: next(i for i, s in enumerate(code) if i >= start and needle in s)
    first = idx("!= \"AAPL\"")
    ask1 = idx("vector_search(question, ticker=\"AAPL\")", first)
    load2 = idx("load_chunks(later)", ask1)
    ask2 = idx("vector_search(question, ticker=\"AAPL\")", load2)
    assert first < ask1 < load2 < ask2 and "ON CONFLICT (id) DO UPDATE" in "\n".join(code)


# ---- the pack ------------------------------------------------------------------------------------------------------------------

class FakeDbx:
    def __init__(self, endpoints=("databricks-gte-large-en", "databricks-meta-llama-3-3-70b-instruct")):
        self.profile = None
        self.endpoints = endpoints
        self.calls, self.captured, self.deleted = [], None, []

    def profiles(self):
        return [{"name": "DEFAULT", "host": "https://dbc-test.cloud.databricks.com", "default": True, "valid": True}]

    def json(self, *a, body=None):
        self.calls.append(a)
        if a[:2] == ("current-user", "me"):
            return {"userName": "learner@example.com"}
        if a[0] == "serving-endpoints":
            return [{"name": n} for n in self.endpoints]
        if a[:2] == ("workspace", "list"):
            return [{"object_type": "NOTEBOOK", "path": f"{a[2]}/{p.stem}"} for p in tp.NOTEBOOKS.glob("*.py")] + [{"object_type": "DIRECTORY", "path": f"{a[2]}/app"}]
        if a[:2] == ("workspace", "get-status"):
            return {"object_id": 123456}
        return {}

    def text(self, *a, stdin=None):
        self.calls.append(a)
        if a[:2] == ("workspace", "import-dir"):
            self.captured = Path(tempfile.mkdtemp()) / "captured"        # outside the pack's own temp dir, which is deleted after the step
            shutil.copytree(a[2], self.captured)
            self.captured_path = a[3]
        if a[:2] == ("workspace", "delete"):
            self.deleted.append(a[2])
        return ""


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    rows = [{"id": f"AAPL-2025-1A-{i}", "title": "t", "content": "c", "meta": {"ticker": "AAPL", "company": "Apple Inc.", "fiscal_year": "2025", "item": "1A", "section": "Risk Factors"}}
            for i in range(3)] + [{"id": "WMT-2025-7-1", "title": "t", "content": "c", "meta": {"ticker": "WMT", "company": "Walmart Inc.", "fiscal_year": "2025", "item": "7", "section": "MD&A"}}]
    monkeypatch.setattr(tp.datasets, "fetch_snapshot", lambda *a, **k: rows)


def launch(fake, **params):
    lines = []
    env = run_pack(tp.RagSecTutorial(), "launch", "demo", params, services={"dbx": fake}, log=lines.append)
    return env, "\n".join(lines)


def test_registered_with_aliases_and_the_existing_packs_still_resolve():
    assert registry.resolve("rag-sec-tutorial").id == "rag-sec-tutorial" and registry.resolve("rag-tutorial").id == "rag-sec-tutorial"
    assert registry.resolve("rag-sec").id == "rag-sec-filings"                       # an existing alias must not change meaning


def test_launch_uploads_nine_filled_in_notebooks_and_the_app(tmp_path):
    fake = FakeDbx()
    env, out = launch(fake)
    assert env.exit_code == 0
    assert fake.captured_path == "/Users/learner@example.com/labagent-rag-tutorial-demo"
    names = sorted(p.stem for p in fake.captured.glob("*.py"))
    assert names == sorted(NAMES)
    common = (fake.captured / "_common.py").read_text()
    entry = datasets.registry()[tp.DATASET]
    assert entry["sha256"] in common and '"/Workspace/Users/learner@example.com/labagent-rag-tutorial-demo"' in common
    assert not re.search(r"__[A-Z_]{4,}__", common)
    assert {p.name for p in (fake.captured / "app").iterdir()} >= {"app.py", "retrieval.py", "app.yaml", "requirements.txt", "ui.json", "static"}
    ui = json.loads((fake.captured / "app" / "ui.json").read_text())
    assert ui and "AAPL" in json.dumps(ui)                                              # the UI menu was built from the dataset
    assert not list(fake.captured.rglob("__pycache__"))
    assert "browse/folders/123456" in out and "00_setup" in out
    assert load_state("rag-sec-tutorial", "demo")["workspace_path"] == fake.captured_path


def test_launch_creates_nothing_in_databricks_except_the_folder():
    fake = FakeDbx()
    launch(fake)
    verbs = {a[:2] for a in fake.calls if a[0] in ("workspace", "postgres", "apps")}
    assert verbs <= {("workspace", "mkdirs"), ("workspace", "import-dir"), ("workspace", "list"), ("workspace", "get-status")}


def test_missing_model_endpoints_fail_before_uploading_anything():
    fake = FakeDbx(endpoints=("some-other-endpoint",))
    env, _ = launch(fake)
    assert env.exit_code != 0 and "embedding or chat" in str(env.__dict__) and fake.captured is None


def test_unknown_dataset_is_rejected():
    env, _ = launch(FakeDbx(), dataset="sec-10k-1999.01")
    assert env.exit_code != 0 and "unknown dataset" in str(env.__dict__)


def test_a_missing_notebook_is_reported_by_the_verify_step():
    fake = FakeDbx()
    real = fake.json
    fake.json = lambda *a, body=None: ([x for x in real(*a) if not x["path"].endswith("04_search")] if a[:2] == ("workspace", "list") else real(*a, body=body))
    env, _ = launch(fake)
    assert env.exit_code != 0 and "04_search" in str(env.__dict__)


def test_destroy_deletes_the_folder_and_reminds_about_the_cleanup_notebook():
    fake = FakeDbx()
    launch(fake)
    lines = []
    env = run_pack(tp.RagSecTutorial(), "destroy", "demo", {}, services={"dbx": fake}, log=lines.append)
    assert env.exit_code == 0 and fake.deleted == ["/Users/learner@example.com/labagent-rag-tutorial-demo"]
    assert "99_cleanup" in "\n".join(lines)
    again = run_pack(tp.RagSecTutorial(), "destroy", "demo", {}, services={"dbx": fake})
    assert again.exit_code == 0 and len(fake.deleted) == 1                                # safe to run twice


def test_notebooks_ship_in_the_wheel():
    root = Path(__file__).parent.parent
    assert "packs/*/tutorial/notebooks/*.py" in (root / "pyproject.toml").read_text()
    assert (root / "labagent" / "packs" / "rag" / "tutorial" / "__init__.py").exists()
