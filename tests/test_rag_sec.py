import importlib.util
import json
from pathlib import Path

import pytest

from labagent.packs.base import PackContext, PackError, run_pack
from labagent.packs.rag.chunking import chunk_text
from labagent.packs.rag.sources.sec import SecFilings, html_to_text, split_items

APP = Path(__file__).parent.parent / "labagent" / "packs" / "rag" / "app"


def _retrieval():
    """The retrieval module ships inside the app bundle, so tests load it by path."""
    spec = importlib.util.spec_from_file_location("labagent_rag_retrieval", APP / "retrieval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PARA = "The company faces significant risks from supply chain disruption and component shortages. " * 6


def filing(name="Acme Corp"):
    return f"""<html><body>
<div style="display:none"><ix:header><ix:hidden>0000320193 FY secret-junk-xbrl</ix:hidden></ix:header></div>
<p>TABLE OF CONTENTS</p>
<p>Item 1. Business</p><p>Item 1A. Risk Factors</p><p>Item 7. Management's Discussion</p>
<p>Item 1. Business</p>
<p>{name} designs and sells widgets. {PARA}</p><p>{PARA}</p>
<p>Item 1A. Risk Factors</p>
<p>{PARA}</p><p>{PARA}</p><p>{PARA}</p>
<p>Item 1B. Unresolved Staff Comments</p><p>None.</p>
<p>Item 7. Management&#8217;s Discussion and Analysis</p>
<p>Net sales increased 7 percent. {PARA}</p><p>{PARA}</p>
<p>Item 8. Financial Statements</p><p>Statements follow.</p>
</body></html>"""


def test_html_to_text_drops_xbrl_header_and_keeps_structure():
    t = html_to_text(filing())
    assert "secret-junk" not in t and "Item 1A. Risk Factors" in t


def test_split_items_skips_table_of_contents_and_stubs():
    items = split_items(html_to_text(filing()), ["1", "1A", "7", "1B"])
    assert set(items) == {"1", "1A", "7"}            # 1B is a 'None.' stub, TOC lines are not sections
    assert "supply chain" in items["1A"] and "Unresolved" not in items["1A"]
    assert "Net sales increased" in items["7"]


def test_chunks_respect_size_and_keep_paragraphs():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(12))
    chunks = chunk_text(text, max_chars=700)
    assert all(len(c) <= 1100 for c in chunks) and len(chunks) > 3
    assert "Paragraph 0." in chunks[0] and all("Paragraph" in c for c in chunks)
    long = "Sentence number one is here. " * 100          # one huge paragraph is split on sentences
    assert all(len(c) <= 700 for c in chunk_text(long, max_chars=700))


def sec_ctx(tmp_path, monkeypatch, tickers="ACME", **extra):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    pages = {
        "https://www.sec.gov/files/company_tickers.json": json.dumps({"0": {"cik_str": 111, "ticker": "ACME", "title": "Acme Corp"}}),
        "https://data.sec.gov/submissions/CIK0000000111.json": json.dumps({"name": "Acme Corp", "filings": {"recent": {
            "form": ["8-K", "10-K", "10-K"], "accessionNumber": ["0001-24-1", "0001-24-2", "0001-23-3"],
            "primaryDocument": ["x.htm", "acme-2024.htm", "acme-2023.htm"],
            "reportDate": ["", "2024-12-31", "2023-12-31"], "filingDate": ["2025-01-05", "2025-02-01", "2024-02-01"]}}}),
        "https://www.sec.gov/Archives/edgar/data/111/000124 2/acme-2024.htm".replace(" ", ""): filing(),
        "https://www.sec.gov/Archives/edgar/data/111/000123 3/acme-2023.htm".replace(" ", ""): filing(),
    }
    seen = []

    def http(url, ua):
        seen.append((url, ua))
        return pages[url].encode()
    params = {"tickers": tickers, "filings": 2, "sections": "1,1A,7", "user_agent": "Test Person test@example.com", **extra}
    return PackContext("sec-source", "t", params, services={"http": http}), seen


def test_sec_source_builds_cited_chunks_with_metadata(tmp_path, monkeypatch):
    ctx, seen = sec_ctx(tmp_path, monkeypatch)
    chunks = SecFilings().chunks(ctx)
    assert {c["meta"]["fiscal_year"] for c in chunks} == {"2023", "2024"}       # the 8-K was ignored
    assert {c["meta"]["item"] for c in chunks} == {"1", "1A", "7"}
    m = chunks[0]["meta"]
    assert m["ticker"] == "ACME" and m["form"] == "10-K" and m["url"].startswith("https://www.sec.gov/Archives/edgar/data/111/")
    assert len({c["id"] for c in chunks}) == len(chunks)
    assert all(ua == "Test Person test@example.com" for _, ua in seen)
    n = len(seen)
    SecFilings().chunks(sec_ctx(tmp_path, monkeypatch)[0])                       # second run is served from the cache
    assert n == 4


def test_sec_requires_contact_and_limits_volume(tmp_path, monkeypatch):
    ctx, _ = sec_ctx(tmp_path, monkeypatch, user_agent="nobody")
    with pytest.raises(PackError, match="contact"):
        SecFilings().chunks(ctx)
    ctx, _ = sec_ctx(tmp_path, monkeypatch, tickers=",".join(["ACME"] * 13))
    with pytest.raises(PackError, match="25 filings"):
        SecFilings().chunks(ctx)
    ctx, _ = sec_ctx(tmp_path, monkeypatch, tickers="NOPE")
    with pytest.raises(PackError, match="ticker not found"):
        SecFilings().chunks(ctx)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, vec, fts):
        self.vec, self.fts, self.sql = vec, fts, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return Rows(self.fts if "websearch_to_tsquery" in sql else self.vec)


def test_hybrid_search_fuses_rankings_and_filters():
    r = _retrieval()
    a, b, c = ("a", "A", "x", {"ticker": "AAPL", "item": "1A"}), ("b", "B", "y", {"ticker": "JPM"}), ("c", "C", "z", {"ticker": "AAPL"})
    conn = Conn(vec=[a, b, c], fts=[c, a])
    res = r.search(conn, "rag_sec", [0.1, 0.2], "supply chain", k=2, filters={"ticker": "AAPL"})
    assert [x["id"] for x in res] == ["a", "c"]        # a: vec 1 + fts 2; c: vec 3 + fts 1; b only in vec
    assert all("meta @> %s::jsonb" in s for s, _ in conn.sql)
    assert json.loads(conn.sql[0][1][0]) == {"ticker": "AAPL"}
    assert r.is_hit(res, {"ticker": "AAPL", "item": "1A"}) and not r.is_hit(res, {"ticker": "JPM"})


def test_search_rejects_bad_schema_name():
    with pytest.raises(ValueError):
        _retrieval().search(Conn([], []), "rag; drop table x", [0.1], "q")


def test_eval_set_covers_only_loaded_tickers_and_is_well_formed():
    qs = SecFilings().eval_set()
    assert len(qs) >= 10 and all(q["question"] and q["expect"].get("ticker") and q["expect"].get("item") for q in qs)


def test_older_submission_pages_are_read_when_recent_has_too_few_10ks(tmp_path, monkeypatch):
    ctx, seen = sec_ctx(tmp_path, monkeypatch)
    pages = {}
    base_http = ctx.services["http"]
    recent = {"form": ["8-K", "10-K"], "accessionNumber": ["0001-24-1", "0001-24-2"], "primaryDocument": ["x.htm", "acme-2024.htm"],
              "reportDate": ["", "2024-12-31"], "filingDate": ["2025-01-05", "2025-02-01"]}
    older = {"form": ["4", "10-K"], "accessionNumber": ["0001-23-9", "0001-23-3"], "primaryDocument": ["f4.xml", "acme-2023.htm"],
             "reportDate": ["", "2023-12-31"], "filingDate": ["2024-03-01", "2024-02-01"]}

    def http(url, ua):
        if url.endswith("CIK0000000111.json"):
            return json.dumps({"name": "Acme Corp", "filings": {"recent": recent, "files": [{"name": "CIK0000000111-submissions-001.json"}]}}).encode()
        if url.endswith("-submissions-001.json"):
            return json.dumps(older).encode()
        return base_http(url, ua)
    ctx.services["http"] = http
    chunks = SecFilings().chunks(ctx)
    assert {c["meta"]["fiscal_year"] for c in chunks} == {"2023", "2024"}


def test_ticker_can_be_pinned_to_a_cik_and_missing_10k_explains_why(tmp_path, monkeypatch):
    ctx, seen = sec_ctx(tmp_path, monkeypatch, tickers="ACME=111")
    assert SecFilings().chunks(ctx)
    assert not any("company_tickers" in u for u, _ in seen)        # a pinned CIK needs no directory lookup
    ctx, _ = sec_ctx(tmp_path / "fresh-cache", monkeypatch, tickers="ACME=111", filings=1)
    ctx.services["http"] = lambda url, ua: json.dumps({"name": "New Holdco", "filings": {"recent": {
        "form": ["8-K"], "accessionNumber": ["1"], "primaryDocument": ["a"], "reportDate": [""], "filingDate": ["2026-07-01"]}}}).encode()
    with pytest.raises(PackError, match="older CIK"):
        SecFilings().chunks(ctx)
