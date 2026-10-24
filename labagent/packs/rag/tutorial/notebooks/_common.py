# Databricks notebook source
# MAGIC %md
# MAGIC # Shared settings and helpers
# MAGIC
# MAGIC You do not run this notebook yourself. Every other notebook starts with `%run ./_common`, which runs this file first.
# MAGIC
# MAGIC It holds the names used across the tutorial, the dataset download (checked against a SHA-256), and a few small helpers. Read it once so nothing feels like magic, then ignore it.

# COMMAND ----------

import gzip
import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
ME = w.current_user.me().user_name

# COMMAND ----------

# Names used by every notebook. Change PROJECT only if that name is already taken in your workspace.
PROJECT = "rag-tutorial"
BRANCH = f"projects/{PROJECT}/branches/production"
LAKEBASE_ENDPOINT = f"{BRANCH}/endpoints/primary"
SCHEMA = "tutorial"
APP_NAME = "rag-tutorial"
TOP_K = 4

# The dataset. The download is verified against this SHA-256, so everyone works on identical data.
SNAPSHOT_URL = "__SNAPSHOT_URL__"
SNAPSHOT_SHA256 = "__SNAPSHOT_SHA256__"

# Where labagent uploaded this tutorial (notebook 06 reads the app code from here).
TUTORIAL_FOLDER = "__WORKSPACE_FOLDER__"

BATCH = 8            # texts per embedding request: small batches avoid the endpoint's rate limit
EMBED_CHARS = 1800   # the embedding model accepts about 512 tokens, so cut longer text

# COMMAND ----------

# Model serving endpoints. Every Databricks workspace has pay-per-token foundation models.
_names = [e.name for e in w.serving_endpoints.list()]
_is_embedding = lambda n: re.search(r"gte|bge|embed", n, re.I)
EMBED_ENDPOINT = "databricks-gte-large-en" if "databricks-gte-large-en" in _names else next((n for n in _names if _is_embedding(n)), None)
CHAT_ENDPOINT = next((n for n in _names if re.search(r"llama|gpt|claude|gemma|qwen|instruct|chat", n, re.I) and not _is_embedding(n)), None)


def check(condition, message):
    """Stop with a clear message when a step did not work. Each notebook ends with checks like this."""
    if not condition:
        raise AssertionError(f"CHECK FAILED: {message}")
    print(f"OK: {message}")


# COMMAND ----------

def load_rows():
    """The SEC 10-K chunks, downloaded once to local disk and verified. Returns a list of dicts: id, title, content, meta."""
    path = Path("/tmp/sec-10k.chunks.jsonl.gz")
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    if not path.exists() or sha(path) != SNAPSHOT_SHA256:
        req = urllib.request.Request(SNAPSHOT_URL, headers={"User-Agent": "labagent-tutorial"})
        with urllib.request.urlopen(req, timeout=120) as r:
            path.write_bytes(r.read())
    if sha(path) != SNAPSHOT_SHA256:
        raise RuntimeError("the downloaded dataset does not match its checksum: do not use it")
    return [json.loads(line) for line in gzip.open(path, "rt")]


def embed(texts):
    """Embedding vectors for a list of texts. Sends small batches and waits when the endpoint says to slow down."""
    out = []
    for i in range(0, len(texts), BATCH):
        batch = [t[:EMBED_CHARS] for t in texts[i:i + BATCH]]
        for attempt in range(8):
            try:
                resp = w.api_client.do("POST", f"/serving-endpoints/{EMBED_ENDPOINT}/invocations", body={"input": batch})
                out += [d["embedding"] for d in resp["data"]]
                break
            except Exception as e:
                if not re.search(r"REQUEST_LIMIT_EXCEEDED|429|rate limit|too many", str(e), re.I) or attempt == 7:
                    raise
                time.sleep(min(30, 2 ** attempt))
    return out


def vec(v):
    """A vector as the text Postgres expects: [0.1,0.2,...]"""
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# COMMAND ----------

def _find(obj, *keys):
    """First value found under any of `keys`, searching nested dicts and lists."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _find(v, *keys)
            if r not in (None, ""):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, *keys)
            if r not in (None, ""):
                return r
    return None


def lakebase_host():
    """The hostname of the Lakebase compute endpoint, or None while it is still starting."""
    eps = w.api_client.do("GET", f"/api/2.0/postgres/{BRANCH}/endpoints").get("endpoints", [])
    return _find(eps[0], "host") if eps else None


def pg():
    """A Postgres connection to Lakebase. The password is a short-lived token, so every call asks for a fresh one."""
    import psycopg
    token = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": LAKEBASE_ENDPOINT})["token"]
    return psycopg.connect(host=lakebase_host(), port=5432, dbname="databricks_postgres", user=ME, password=token,
                           sslmode="require", autocommit=True, connect_timeout=30)


# COMMAND ----------

RRF_K = 60


def hybrid_search(conn, qvec, qtext, k=TOP_K, filters=None, pool=20):
    """Vector search plus full-text search, merged with reciprocal rank fusion. Notebook 04 builds this up one piece at a time."""
    where, fargs = "", []
    if filters:
        where, fargs = " AND meta @> %s::jsonb", [json.dumps({a: str(b) for a, b in filters.items()})]
    by_vector = conn.execute(f"SELECT id, title, content, meta FROM {SCHEMA}.chunks WHERE true{where} "
                             "ORDER BY embedding <=> %s::vector LIMIT %s", (*fargs, vec(qvec), pool)).fetchall()
    by_text = conn.execute(f"SELECT id, title, content, meta FROM {SCHEMA}.chunks, websearch_to_tsquery('english', %s) q "
                           f"WHERE tsv @@ q{where} ORDER BY ts_rank_cd(tsv, q) DESC LIMIT %s", (qtext, *fargs, pool)).fetchall()
    score, rows = {}, {}
    for ranked in (by_vector, by_text):
        for rank, r in enumerate(ranked, 1):
            score[r[0]] = score.get(r[0], 0.0) + 1.0 / (RRF_K + rank)
            rows[r[0]] = r
    return [{"id": i, "title": rows[i][1], "content": rows[i][2], "meta": rows[i][3] if isinstance(rows[i][3], dict) else json.loads(rows[i][3] or "{}"),
             "score": round(score[i], 5)} for i in sorted(score, key=score.get, reverse=True)[:k]]


def is_hit(results, expect):
    """True when any result has all the expected metadata values (for example ticker AAPL and item 1A)."""
    return any(all(str(r["meta"].get(k)) == str(v) for k, v in expect.items()) for r in results)


print(f"ready: signed in as {ME} | embeddings: {EMBED_ENDPOINT} | chat: {CHAT_ENDPOINT}")
