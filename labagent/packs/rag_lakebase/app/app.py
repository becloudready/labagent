"""Minimal RAG service: embed the question, retrieve with pgvector, answer from the retrieved passages only."""
import os
import time

import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

w = WorkspaceClient()
app = FastAPI(title="labagent RAG")
_tok = {"v": None, "exp": 0.0}


def _token() -> str:
    if time.time() > _tok["exp"]:   # tokens last 1 hour: refresh after 50 minutes
        cred = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": os.environ["LAKEBASE_ENDPOINT"]})
        _tok.update(v=cred["token"], exp=time.time() + 3000)
    return _tok["v"]


def _db():
    return psycopg.connect(host=os.environ["PGHOST"], dbname=os.environ["PGDATABASE"], sslmode="require",
                           user=w.config.client_id, password=_token(), connect_timeout=30)


def _invoke(endpoint: str, body: dict) -> dict:
    return w.api_client.do("POST", f"/serving-endpoints/{endpoint}/invocations", body=body)


class Ask(BaseModel):
    question: str


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>RAG on Lakebase</title><body style="font:16px system-ui;max-width:720px;margin:2rem auto;padding:0 1rem">
<h1>Ask the knowledge base</h1><form id=f><input id=q style="width:75%;padding:.5rem" placeholder="How long do refunds take?">
<button>Ask</button></form><pre id=a style="white-space:pre-wrap"></pre><ol id=s></ol>
<script>f.onsubmit=async e=>{e.preventDefault();a.textContent='...';
const r=await(await fetch('/ask',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question:q.value})})).json();
a.textContent=r.answer;s.innerHTML=r.sources.map(x=>`<li>${x.title} (${x.score.toFixed(2)})</li>`).join('')}</script>"""


@app.get("/health")
def health():
    with _db() as c:
        return {"chunks": c.execute("select count(*) from rag.chunks").fetchone()[0]}


@app.post("/ask")
def ask(a: Ask):
    vec = _invoke(os.environ["EMBED_ENDPOINT"], {"input": [a.question]})["data"][0]["embedding"]
    lit = "[" + ",".join(str(x) for x in vec) + "]"
    with _db() as c:
        rows = c.execute("select title, content, 1 - (embedding <=> %s::vector) from rag.chunks "
                         "order by embedding <=> %s::vector limit %s", (lit, lit, int(os.environ.get("TOP_K", "4")))).fetchall()
    context = "\n".join(f"- {t}: {body}" for t, body, _ in rows)
    msg = [{"role": "system", "content": "Answer using only the context. If it is not there, say you do not know."},
           {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {a.question}"}]
    out = _invoke(os.environ["CHAT_ENDPOINT"], {"messages": msg, "max_tokens": 300})
    return {"answer": out["choices"][0]["message"]["content"],
            "sources": [{"title": t, "score": float(s)} for t, _, s in rows]}
