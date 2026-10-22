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
        Param("tickers", "AAPL,JPM,PFE,XOM,WMT", "Comma-separated tickers"),
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

    def _json(self, ctx: PackContext, url: str) -> Any:
        return json.loads(self._get(ctx, url))

    # -- source --------------------------------------------------------------
    def chunks(self, ctx: PackContext) -> List[Dict[str, Any]]:
        ua = str(ctx.params["user_agent"] or "")
        if "@" not in ua:
            raise PackError("EDGAR requires a contact in the User-Agent, as 'Full Name address' with a real email address, e.g. --param \"user_agent=...\"")
        tickers = [t.strip().upper() for t in str(ctx.params["tickers"]).split(",") if t.strip()]
        wanted = [s.strip().upper() for s in str(ctx.params["sections"]).split(",") if s.strip()]
        n = max(1, int(ctx.params["filings"]))
        if len(tickers) * n > 25:
            raise PackError("more than 25 filings requested: EDGAR asks for moderate use, and Free Edition storage is small")
        directory = self._json(ctx, "https://www.sec.gov/files/company_tickers.json")
        by_ticker = {v["ticker"].upper(): v for v in directory.values()}
        out: List[Dict[str, Any]] = []
        for t in tickers:
            if t not in by_ticker:
                raise PackError(f"ticker not found in EDGAR: {t}")
            cik, name = int(by_ticker[t]["cik_str"]), by_ticker[t]["title"]
            sub = self._json(ctx, f"https://data.sec.gov/submissions/CIK{cik:010d}.json")["filings"]["recent"]
            rows = [i for i, f in enumerate(sub["form"]) if f == "10-K"][:n]
            if not rows:
                raise PackError(f"no 10-K found for {t}")
            for i in rows:
                acc, doc = sub["accessionNumber"][i], sub["primaryDocument"][i]
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
                text = html_to_text(self._get(ctx, url).decode("utf-8", "replace"))
                fy = (sub["reportDate"][i] or sub["filingDate"][i])[:4]
                sections = split_items(text, wanted)
                if not sections:
                    ctx.log(f"warning: no sections parsed for {t} {fy}")
                for item, body in sections.items():
                    meta = {"ticker": t, "company": name, "form": "10-K", "fiscal_year": fy, "item": item,
                            "section": SECTION_NAMES.get(item, f"Item {item}"), "filed": sub["filingDate"][i], "url": url}
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
            {"question": "How did Walmart's comparable sales and operating income perform?", "expect": {"ticker": "WMT", "item": "7"}},
            {"question": "What competition and supply risks does Walmart face?", "expect": {"ticker": "WMT", "item": "1A"}},
        ]
