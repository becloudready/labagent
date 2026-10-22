import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

from labagent.packs.rag.sources.builtin import Builtin
from labagent.packs.rag.sources.sec import SecFilings

APP = Path(__file__).parent.parent / "labagent" / "packs" / "rag" / "app"
METAS = [{"ticker": t, "company": c} for t, c in [("AAPL", "Apple Inc."), ("JPM", "JPMorgan Chase"), ("PFE", "Pfizer"), ("WMT", "Walmart"), ("CVX", "Chevron")]]


def test_sec_ui_suggestions_cover_loaded_companies_with_mixed_topics():
    ui = SecFilings().ui(METAS)
    s = ui["suggestions"]
    assert [f["key"] for f in ui["facets"]] == ["ticker", "fiscal_year", "section"]
    assert sum(1 for x in s if "filters" not in x) == 2                       # cross-company questions
    first = [x["label"].split(" · ")[0] for x in s[2:7]]
    assert len(set(first)) == 3                                                # the first cards mix topics
    assert all(x["filters"]["ticker"] in {m["ticker"] for m in METAS} for x in s if "filters" in x)
    only = SecFilings().ui([METAS[0]])["suggestions"]
    assert all("JPMorgan" not in x["question"] for x in only)                  # nothing about companies that are not loaded


def test_builtin_ui_only_suggests_loaded_topics():
    ui = Builtin().ui([{"source": "returns"}])
    assert ui["suggestions"] and all(x["filters"] == {"source": "returns"} for x in ui["suggestions"])


def test_page_is_self_contained_and_escapes_untrusted_text():
    html = (APP / "static" / "index.html").read_text()
    assert not re.search(r'(src|href)="https?://', html)                       # no CDN or external assets
    assert "<script src" not in html and "innerHTML" in html and "const esc=" in html
    assert "/^https?:\\/\\//.test(s.url" in html                               # only http(s) links are rendered


@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = lambda: types.SimpleNamespace(config=types.SimpleNamespace(client_id="sp"), api_client=None)
    monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setitem(sys.modules, "psycopg", types.ModuleType("psycopg"))
    monkeypatch.setenv("RAG_SCHEMA", "rag_sec")
    monkeypatch.syspath_prepend(str(APP))
    sys.modules.pop("retrieval", None)
    spec = importlib.util.spec_from_file_location("rag_app_under_test", APP / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ui = SecFilings().ui(METAS)
    (APP / "ui.json").write_text(json.dumps(ui))

    class R:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "count(distinct" in sql:
                return R([(10,)])
            if "count(*)" in sql:
                return R([(1842,)])
            if "group by 1" in sql:
                return R([("AAPL", "Apple Inc."), ("JPM", "JPMorgan Chase")] if params[0] == "ticker" else [("2024", "2024"), ("2025", "2025")])
            if "websearch_to_tsquery" in sql:
                return R([])
            return R([("c1", "Apple 2025 10-K, Item 1A", "Tariffs could raise costs. " * 40,
                       {"ticker": "AAPL", "company": "Apple Inc.", "fiscal_year": "2025", "section": "Risk Factors",
                        "url": "https://www.sec.gov/x", "secret_internal": "no"})])
    mod._db = lambda: Conn()
    api = types.SimpleNamespace(do=lambda method, path, body=None: (
        {"data": [{"embedding": [0.1, 0.2]}]} if "embed" in path else {"choices": [{"message": {"content": "Answer [1]."}}]}))
    mod.w.api_client = api
    monkeypatch.setenv("EMBED_ENDPOINT", "embed")
    monkeypatch.setenv("CHAT_ENDPOINT", "chat")
    from fastapi.testclient import TestClient
    yield TestClient(mod.app)
    (APP / "ui.json").unlink(missing_ok=True)


def test_app_serves_page_ui_meta_and_cited_answers(client):
    assert client.get("/").status_code == 200 and "<title>" in client.get("/").text
    assert client.get("/ui.json").json()["title"] == "Filings Research Assistant"
    m = client.get("/meta").json()
    assert m["chunks"] == 1842 and m["documents"] == 10
    assert [v["value"] for v in m["facets"]["fiscal_year"]] == ["2025", "2024"]          # newest year first
    assert m["facets"]["ticker"][0]["label"] == "AAPL: Apple Inc."
    r = client.post("/ask", json={"question": "tariffs?", "filters": {"ticker": "AAPL"}}).json()
    s = r["sources"][0]
    assert r["answer"] == "Answer [1]." and s["n"] == 1 and len(s["snippet"]) <= 600
    assert s["meta"]["fiscal_year"] == "2025" and "secret_internal" not in s["meta"]      # only whitelisted metadata is exposed
