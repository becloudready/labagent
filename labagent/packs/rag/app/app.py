"""RAG service: hybrid retrieval over Lakebase, answers only from retrieved passages, with numbered citations."""
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional

import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from retrieval import search

w = WorkspaceClient()
app = FastAPI(title="labagent RAG")
SCHEMA = os.environ["RAG_SCHEMA"]
HERE = Path(__file__).parent
_tok = {"v": None, "exp": 0.0}
_meta = {"v": None, "exp": 0.0}
KEY = re.compile(r"^[a-z_][a-z0-9_]*$")
SHOWN_META = ("ticker", "company", "fiscal_year", "section", "item", "form", "url", "source")

SYSTEM = ("You answer questions using only the numbered context passages. Cite the passages you used like [1] or [2]. "
          "If the context does not contain the answer, say you do not know. Do not use outside knowledge.")


def _token() -> str:
    if time.time() > _tok["exp"]:   # tokens last 1 hour: refresh after 50 minutes
        cred = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": os.environ["LAKEBASE_ENDPOINT"]})
        _tok.update(v=cred["token"], exp=time.time() + 3000)
    return _tok["v"]


def _db():
    return psycopg.connect(host=os.environ["PGHOST"], dbname=os.environ["PGDATABASE"], sslmode="require", autocommit=True,
                           user=w.config.client_id, password=_token(), connect_timeout=30)


def _invoke(endpoint: str, body: dict) -> dict:
    return w.api_client.do("POST", f"/serving-endpoints/{endpoint}/invocations", body=body)


class Ask(BaseModel):
    question: str
    filters: Optional[Dict[str, str]] = None    # e.g. {"ticker": "AAPL", "fiscal_year": "2024"}


def _ui() -> dict:
    f = HERE / "ui.json"
    return json.loads(f.read_text()) if f.exists() else {}


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/ui.json")
def ui():
    return JSONResponse(_ui())


@app.get("/meta")
def meta():
    """Dataset size and the values available for each filter, so the menu always matches what is loaded."""
    if _meta["v"] and time.time() < _meta["exp"]:
        return _meta["v"]
    facets = {}
    with _db() as c:
        total = c.execute(f"select count(*) from {SCHEMA}.chunks").fetchone()[0]
        for f in _ui().get("facets", []):
            key, label_key = f["key"], f.get("label_key", f["key"])
            if not (KEY.match(key) and KEY.match(label_key)):
                continue
            rows = c.execute(f"select meta->>%s, max(meta->>%s) from {SCHEMA}.chunks where meta ? %s group by 1 order by 1",
                             (key, label_key, key)).fetchall()
            vals = [{"value": v, "label": (f"{v}: {lab}" if lab and lab != v else v)} for v, lab in rows if v]
            facets[key] = sorted(vals, key=lambda x: x["value"], reverse=bool(f.get("desc")))
        docs = c.execute(f"select count(distinct meta->>'url') from {SCHEMA}.chunks where meta ? 'url'").fetchone()[0]
    _meta.update(v={"chunks": total, "documents": docs or None, "facets": facets}, exp=time.time() + 60)
    return _meta["v"]


@app.get("/health")
def health():
    with _db() as c:
        return {"chunks": c.execute(f"select count(*) from {SCHEMA}.chunks").fetchone()[0]}


@app.post("/ask")
def ask(a: Ask):
    vec = _invoke(os.environ["EMBED_ENDPOINT"], {"input": [a.question]})["data"][0]["embedding"]
    with _db() as c:
        hits = search(c, SCHEMA, vec, a.question, int(os.environ.get("TOP_K", "4")), a.filters)
    if not hits:
        return {"answer": "I do not know: no matching passages.", "sources": []}
    context = "\n\n".join(f"[{i}] {h['title']}\n{h['content']}" for i, h in enumerate(hits, 1))
    out = _invoke(os.environ["CHAT_ENDPOINT"], {"messages": [{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {a.question}"}], "max_tokens": 500})
    return {"answer": out["choices"][0]["message"]["content"],
            "sources": [{"n": i, "title": h["title"], "url": h["meta"].get("url"), "score": h["score"],
                         "snippet": h["content"][:600], "meta": {k: v for k, v in h["meta"].items() if k in SHOWN_META}}
                        for i, h in enumerate(hits, 1)]}
