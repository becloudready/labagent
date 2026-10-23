"""SEC EDGAR 10-K filings: download, parse the HTML, split into Items, chunk by section.

EDGAR rules honoured here: a declared User-Agent with a real contact, at most ~8 requests per second, no crawling
(only the few filings you ask for), and a local cache so a re-run downloads nothing. Filings are fetched at launch
and never bundled with labagent.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...base import PackContext, PackError, Param
from ..chunking import make_chunks
from . import Source

SECTION_NAMES = {"1": "Business", "1A": "Risk Factors", "7": "Management's Discussion and Analysis",
                 "7A": "Market Risk", "3": "Legal Proceedings", "9A": "Controls and Procedures"}
ITEM_RE = re.compile(r"^\s*item\s+(\d{1,2}[abc]?)\b\s*[\.\:\-–—]?\s*(.*)$", re.I)
BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
SKIP = {"script", "style", "ix:header"}
MAX_PAGES = 15   # older submission pages to read when "recent" holds too few 10-Ks (busy filers like banks)
MIN_GAP = 0.13   # seconds between requests: stays under the SEC limit of 10 per second


def default_http(url: str, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return gzip.decompress(data) if r.headers.get("Content-Encoding") == "gzip" else data


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self._skip += 1
        if tag in BLOCK and not self._skip:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP and self._skip:
            self._skip -= 1
        if tag in BLOCK and not self._skip:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    text = "".join(p.out).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_items(text: str, wanted: List[str]) -> Dict[str, str]:
    """Return {item: text}. A table of contents also matches 'Item 1A', so for each item we keep the occurrence
    with the longest span before the next item heading: that is the real section, not the contents line."""
    lines = text.split("\n")
    heads: List[Tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = ITEM_RE.match(ln)
        if m and len(ln) < 160:
            heads.append((i, m.group(1).upper()))
    best: Dict[str, Tuple[int, int]] = {}
    for n, (i, key) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        if key in wanted and end - i > best.get(key, (0, 0))[1] - best.get(key, (0, 0))[0]:
            best[key] = (i, end)
    out = {}
    for key, (a, b) in best.items():
        body = "\n".join(lines[a + 1:b]).strip()
        if len(body) > 400:     # contents-page stubs and 'see Item 7' pointers are not sections
            out[key] = body
    return out


class SecFilings(Source):
    id = "sec-10k"
    schema = "rag_sec"
    params = [
        Param("tickers", "AAPL,JPM,PFE,WMT,CVX", "Comma-separated tickers; pin an entity with TICKER=CIK (e.g. after a holding-company reorganisation)"),
        Param("filings", 2, "Latest N 10-K filings per company"),
        Param("sections", "1,1A,7", "10-K Items to index (1 Business, 1A Risk Factors, 7 MD&A, 7A, 3, 9A)"),
        Param("user_agent", None, "EDGAR requires your name and email address, e.g. 'Jane Doe jane at example.com' written with a real @", required=True),
    ]

    # -- network -------------------------------------------------------------
    @staticmethod
    def _get(ctx: PackContext, url: str) -> bytes:
        http: Callable[[str, str], bytes] = ctx.services.get("http") or default_http
        cache = Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "cache" / "sec"
        cache.mkdir(parents=True, exist_ok=True)
        f = cache / hashlib.sha256(url.encode()).hexdigest()
        if f.exists():
            return f.read_bytes()
        gap = MIN_GAP - (time.monotonic() - ctx.mem.get("sec_last", 0.0))
        if gap > 0 and not ctx.services.get("http"):
            time.sleep(gap)
        data = http(url, ctx.params["user_agent"])
        ctx.mem["sec_last"] = time.monotonic()
        f.write_bytes(data)
        return data

    @staticmethod
    def _rows(cols: Dict[str, List[str]], limit: int) -> List[Dict[str, str]]:
        idx = [i for i, f in enumerate(cols["form"]) if f == "10-K"][:limit]
        return [{"acc": cols["accessionNumber"][i], "doc": cols["primaryDocument"][i],
                 "report": cols.get("reportDate", [""] * len(cols["form"]))[i], "filed": cols["filingDate"][i]} for i in idx]

    def _tenks(self, ctx: PackContext, cik: int, n: int):
        """Latest n 10-Ks. EDGAR's `recent` block holds only the newest ~1000 filings, which for banks can contain a
        single 10-K, so older pages are read (newest first) until enough are found."""
        sub = self._json(ctx, f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        rows = self._rows(sub["filings"]["recent"], n)
        for page in sub["filings"].get("files", [])[:MAX_PAGES]:
            if len(rows) >= n:
                break
            rows += self._rows(self._json(ctx, f"https://data.sec.gov/submissions/{page['name']}"), n - len(rows))
        return sub.get("name", ""), rows

    def _json(self, ctx: PackContext, url: str) -> Any:
        return json.loads(self._get(ctx, url))

    # -- source --------------------------------------------------------------
    def fingerprint(self, ctx: PackContext) -> str:
        return f"{self.id}:{ctx.params['tickers']}:{ctx.params['filings']}:{ctx.params['sections']}"

    def chunks(self, ctx: PackContext) -> List[Dict[str, Any]]:
        ua = str(ctx.params["user_agent"] or "")
        if "@" not in ua:
            raise PackError("EDGAR requires a contact in the User-Agent, as 'Full Name address' with a real email address, e.g. --param \"user_agent=...\"")
        tickers = [t.strip().upper() for t in str(ctx.params["tickers"]).split(",") if t.strip()]
        wanted = [s.strip().upper() for s in str(ctx.params["sections"]).split(",") if s.strip()]
        n = max(1, int(ctx.params["filings"]))
        if len(tickers) * n > 25:
            raise PackError("more than 25 filings requested: EDGAR asks for moderate use, and Free Edition storage is small")
        directory = None
        out: List[Dict[str, Any]] = []
        for t in tickers:
            t, _, pin = t.partition("=")
            if pin:
                cik = int(pin)
            else:
                if directory is None:
                    directory = {v["ticker"].upper(): v for v in self._json(ctx, "https://www.sec.gov/files/company_tickers.json").values()}
                if t not in directory:
                    raise PackError(f"ticker not found in EDGAR: {t}")
                cik = int(directory[t]["cik_str"])
            name, rows = self._tenks(ctx, cik, n)
            if not rows:
                raise PackError(f"no 10-K found for {t} (CIK {cik}). If the company reorganised, its filings may sit under an older CIK: "
                                f"pin it with --param tickers={t}=<old CIK>")
            for r in rows:
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{r['acc'].replace('-', '')}/{r['doc']}"
                text = html_to_text(self._get(ctx, url).decode("utf-8", "replace"))
                fy = (r["report"] or r["filed"])[:4]
                sections = split_items(text, wanted)
                if not sections:
                    ctx.log(f"warning: no sections parsed for {t} {fy}")
                for item, body in sections.items():
                    meta = {"ticker": t, "company": name, "form": "10-K", "fiscal_year": fy, "item": item,
                            "section": SECTION_NAMES.get(item, f"Item {item}"), "filed": r["filed"], "url": url}
                    out += make_chunks(f"{t}-{fy}-{item}", f"{name} {fy} 10-K, Item {item} {meta['section']}", body, meta)
        if not out:
            raise PackError("no sections could be parsed from the filings")
        return out

    def eval_set(self) -> List[Dict[str, Any]]:
        return [
            {"question": "What are Apple's main supply chain and manufacturing risks?", "expect": {"ticker": "AAPL", "item": "1A"}},
            {"question": "How does Apple describe its products and services?", "expect": {"ticker": "AAPL", "item": "1"}},
            {"question": "What credit and interest rate risks does JPMorgan Chase face?", "expect": {"ticker": "JPM", "item": "1A"}},
            {"question": "How did JPMorgan Chase's net income and revenue change this year?", "expect": {"ticker": "JPM", "item": "7"}},
            {"question": "What patent expirations and generic competition risks does Pfizer disclose?", "expect": {"ticker": "PFE", "item": "1A"}},
            {"question": "What are Pfizer's main product lines and business segments?", "expect": {"ticker": "PFE", "item": "1"}},
            {"question": "How do commodity price swings affect Exxon Mobil's results?", "expect": {"ticker": "XOM", "item": "7"}},
            {"question": "What climate and regulatory risks does Exxon Mobil identify?", "expect": {"ticker": "XOM", "item": "1A"}},
            {"question": "How do oil and gas price changes affect Chevron's earnings?", "expect": {"ticker": "CVX", "item": "7"}},
            {"question": "What climate and regulatory risks does Chevron identify?", "expect": {"ticker": "CVX", "item": "1A"}},
            {"question": "How did Walmart's comparable sales and operating income perform?", "expect": {"ticker": "WMT", "item": "7"}},
            {"question": "What competition and supply risks does Walmart face?", "expect": {"ticker": "WMT", "item": "1A"}},
        ]

    def ui(self, metas):
        companies = {}
        for m in metas:
            companies.setdefault(m["ticker"], m["company"])
        templates = [("Risk Factors", "What are the main risks {c} discloses?", "1A"),
                     ("Results", "How did {c}'s results and operating performance change this year?", "7"),
                     ("Business", "How does {c} describe its business and main products or services?", "1")]
        sugg = [{"label": "Across companies", "question": "Which of these companies mention tariffs or trade restrictions as a risk?"},
                {"label": "Across companies", "question": "How do these companies describe the impact of inflation on their results?"}]
        tick = list(companies)
        # rotate templates across companies so the first cards mix topics and companies
        order = [(t, templates[(i + j) % len(templates)]) for j in range(len(templates)) for i, t in enumerate(tick)]
        sugg += [{"label": f"{tpl[0]} \u00b7 {t}", "question": tpl[1].format(c=companies[t]), "filters": {"ticker": t}} for t, tpl in order]
        return {"title": "Filings Research Assistant", "tagline": "Questions answered from SEC 10-K filings, with citations",
                "heading": "Research the filings", "sub": "Pick a suggestion or ask your own. Narrow the scope on the left.",
                "disclaimer": "Informational only, not investment advice. Source: SEC EDGAR.",
                "facets": [{"key": "ticker", "label": "Company", "kind": "chips", "label_key": "company"},
                           {"key": "fiscal_year", "label": "Fiscal year", "kind": "select", "desc": True},
                           {"key": "section", "label": "Section", "kind": "select"}],
                "suggestions": sugg}


class SecSnapshot(SecFilings):
    """The same corpus from a pinned, checksummed release. No SEC access, no contact needed, identical for everyone."""
    id = "sec-10k-snapshot"
    params = [Param("dataset", "sec-10k-2026.10", "Pinned snapshot (see labagent/packs/rag/sources/datasets.json)")]

    def fingerprint(self, ctx: PackContext) -> str:
        from ....datasets import registry
        entry = registry().get(str(ctx.params["dataset"]), {})
        return f"{self.id}:{ctx.params['dataset']}:{entry.get('sha256', '')[:12]}"

    def chunks(self, ctx: PackContext) -> List[Dict[str, Any]]:
        from ....datasets import DatasetError, default_http, fetch_snapshot
        try:
            return fetch_snapshot(str(ctx.params["dataset"]), download=ctx.services.get("download") or default_http)
        except DatasetError as e:
            raise PackError(str(e)) from e
