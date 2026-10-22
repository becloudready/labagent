"""RAG service: hybrid retrieval over Lakebase, answers only from retrieved passages, with numbered citations."""
import os
import time
from typing import Dict, Optional

import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from retrieval import search

w = WorkspaceClient()
app = FastAPI(title="labagent RAG")
SCHEMA = os.environ["RAG_SCHEMA"]
_tok = {"v": None, "exp": 0.0}

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


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>RAG on Lakebase</title><body style="font:16px system-ui;max-width:780px;margin:2rem auto;padding:0 1rem">
<h1>Ask the documents</h1><form id=f><input id=q style="width:70%;padding:.5rem" placeholder="Ask a question">
<input id=t style="width:12%;padding:.5rem" placeholder="ticker"><button>Ask</button></form>
<pre id=a style="white-space:pre-wrap"></pre><ol id=s></ol>
<script>f.onsubmit=async e=>{e.preventDefault();a.textContent='...';s.innerHTML='';
const fl=t.value?{ticker:t.value.toUpperCase()}:null;
const r=await(await fetch('ask',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question:q.value,filters:fl})})).json();
a.textContent=r.answer;s.innerHTML=r.sources.map(x=>`<li>${x.title} ${x.url?`<a href="${x.url}">source</a>`:''}</li>`).join('')}</script>"""


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
            "sources": [{"n": i, "title": h["title"], "url": h["meta"].get("url"), "score": h["score"]}
                        for i, h in enumerate(hits, 1)]}
