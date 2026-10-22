"""RAG on Databricks Lakebase: one reusable pipeline, pluggable data sources.

Workflow (each stage idempotent, resumes on re-run):
  preflight -> project -> locate -> build (fetch, parse, chunk) -> schema -> load (embed) -> index -> evaluate -> app

`evaluate` is a quality gate: the app is deployed only if retrieval hits the expected passages often enough.
It runs the same hybrid-retrieval code the deployed app uses (app/retrieval.py).
Runs on the learner's machine with their own Databricks CLI profile: labagent never sees their credentials.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Pack, PackContext, PackError, Param, Step, state_dir
from ..databricks import pg as pgmod
from ..databricks.cli import Dbx
from . import sources as src_mod
from .chunking import make_chunks  # noqa: F401  (re-exported for source authors)

HERE = Path(__file__).parent
EMBED_RE = re.compile(r"gte|bge|embed", re.I)
CHAT_RE = re.compile(r"llama|gpt|claude|gemma|qwen|instruct|chat", re.I)
BATCH = 8
EMBED_CHARS = 1800   # gte-large accepts 512 tokens; stay under it


def _retrieval():
    """Load app/retrieval.py by path: it ships inside the app bundle, not as an importable package."""
    spec = importlib.util.spec_from_file_location("labagent_rag_retrieval", HERE / "app" / "retrieval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _items(resp: Any, key: str) -> List[Dict[str, Any]]:
    if isinstance(resp, list):
        return resp
    return (resp or {}).get(key) or []


class RagPack(Pack):
    source_id = ""
    output_keys = ("app_url", "project", "embedding_endpoint", "chat_endpoint", "chunks", "eval_hit_rate", "eval_questions")
    base_params = [
        Param("profile", None, "Databricks CLI profile (default profile if omitted)"),
        Param("project", None, "Lakebase project id (default: rag-<name>). Free Edition allows ONE project per account"),
        Param("embedding_endpoint", None, "Serving endpoint for embeddings (auto-discovered if omitted)"),
        Param("chat_endpoint", None, "Serving endpoint for answers (auto-discovered if omitted)"),
        Param("top_k", 4, "Passages retrieved per question"),
        Param("min_hit_rate", 0.6, "Deploy only if the evaluation hit rate reaches this (0 disables the gate)"),
    ]

    def __init__(self):
        self.source = src_mod.get(self.source_id)
        self.params = self.base_params + list(self.source.params)

    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {"dbx": Dbx(profile), "pg": pgmod.connect}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _project(ctx: PackContext) -> str:
        return ctx.params["project"] or f"rag-{ctx.name}"

    @staticmethod
    def _embed(ctx: PackContext, texts: List[str]) -> List[List[float]]:
        r = ctx.services["dbx"].api("post", f"/serving-endpoints/{ctx.state['embedding_endpoint']}/invocations",
                                    {"input": [t[:EMBED_CHARS] for t in texts]})
        return [d["embedding"] for d in r["data"]]

    @staticmethod
    def _conn(ctx: PackContext):
        d = ctx.services["dbx"]
        tok = pgmod.find(d.json("postgres", "generate-database-credential", ctx.state["lakebase_endpoint"]), "token")
        if not tok:
            raise PackError("could not obtain a Lakebase credential: run `databricks auth login` and retry")
        return ctx.services["pg"](ctx.state["pg_host"], ctx.state["user"], tok)

    def _chunks_file(self, ctx: PackContext) -> Path:
        return state_dir().parent / "cache" / self.id / f"{ctx.name}.jsonl"

    def _read_chunks(self, ctx: PackContext) -> List[Dict[str, Any]]:
        return [json.loads(l) for l in self._chunks_file(ctx).read_text().splitlines() if l.strip()]

    # -- deploy --------------------------------------------------------------
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731
        project = self._project(ctx)
        schema = self.source.schema

        def preflight(c: PackContext):
            me = d().json("current-user", "me")
            user = me.get("userName")
            if not user:
                raise PackError("not authenticated: run `databricks auth login` and retry")
            names = [e.get("name", "") for e in _items(d().json("serving-endpoints", "list"), "endpoints")]
            emb = c.params["embedding_endpoint"] or next((n for n in names if EMBED_RE.search(n)), None)
            chat = c.params["chat_endpoint"] or next((n for n in names if CHAT_RE.search(n) and not EMBED_RE.search(n)), None)
            if not emb or not chat:
                raise PackError("no embedding/chat serving endpoint found in this workspace. "
                                f"Pass --param embedding_endpoint=... --param chat_endpoint=... (available: {', '.join(names[:8]) or 'none'})")
            others = [p.get("name", "") for p in _items(d().json("postgres", "list-projects"), "projects")]
            if others and f"projects/{project}" not in others:
                raise PackError(f"this workspace already has a Lakebase project ({others[0].split('/')[-1]}) and Free Edition allows one. "
                                f"Reuse it with --param project={others[0].split('/')[-1]} or delete it first")
            return {"user": user, "embedding_endpoint": emb, "chat_endpoint": chat, "project": project}

        def mk_project(c: PackContext):
            d().json("postgres", "create-project", project)
            return {"project_created": True}

        def locate(c: PackContext):
            branches = _items(d().json("postgres", "list-branches", f"projects/{project}"), "branches")
            if not branches:
                raise PackError("project has no branch yet; wait a minute and re-run")
            br = next((b for b in branches if pgmod.find(b, "default", "is_default") is True), branches[0])
            eps = _items(d().json("postgres", "list-endpoints", br["name"]), "endpoints")
            host = pgmod.find(eps[0], "host") if eps else None
            if not host:
                raise PackError("compute endpoint not ready yet; re-run in a minute")
            return {"lakebase_endpoint": eps[0]["name"], "pg_host": host}

        def build(c: PackContext):
            chunks = self.source.chunks(c)
            f = self._chunks_file(c)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("\n".join(json.dumps(x) for x in chunks))
            f.chmod(0o600)
            return {"chunks": len(chunks)}

        def sch(c: PackContext):
            dim = len(self._embed(c, ["dimension probe"])[0])
            with self._conn(c) as cx:
                cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cx.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                cx.execute(f"CREATE TABLE IF NOT EXISTS {schema}.chunks (id text PRIMARY KEY, title text, content text, "
                           f"meta jsonb, embedding vector({int(dim)}), "
                           f"tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED)")
            return {"dim": dim}

        def load(c: PackContext):
            chunks = self._read_chunks(c)
            for i in range(0, len(chunks), BATCH):
                batch = chunks[i:i + BATCH]
                vecs = self._embed(c, [f"{x['title']}\n{x['content']}" for x in batch])
                with self._conn(c) as cx:
                    for x, v in zip(batch, vecs):
                        cx.execute(f"INSERT INTO {schema}.chunks (id, title, content, meta, embedding) VALUES (%s,%s,%s,%s::jsonb,%s::vector) "
                                   "ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, content=EXCLUDED.content, "
                                   "meta=EXCLUDED.meta, embedding=EXCLUDED.embedding",
                                   (x["id"], x["title"], x["content"], json.dumps(x["meta"]), _retrieval().vec_literal(v)))
                c.log(f"  embedded {min(i + BATCH, len(chunks))}/{len(chunks)}")

        def loaded(c: PackContext) -> bool:
            try:
                with self._conn(c) as cx:
                    return cx.execute(f"SELECT count(*) FROM {schema}.chunks").fetchone()[0] == c.state.get("chunks")
            except Exception:
                return False

        def index(c: PackContext):
            with self._conn(c) as cx:
                cx.execute(f"CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON {schema}.chunks USING hnsw (embedding vector_cosine_ops)")
                cx.execute(f"CREATE INDEX IF NOT EXISTS chunks_tsv ON {schema}.chunks USING gin (tsv)")
                cx.execute(f"CREATE INDEX IF NOT EXISTS chunks_meta ON {schema}.chunks USING gin (meta jsonb_path_ops)")

        def evaluate(c: PackContext):
            metas = [x["meta"] for x in self._read_chunks(c)]
            qs = [q for q in self.source.eval_set()
                  if any(all(str(m.get(k)) == str(v) for k, v in q["expect"].items()) for m in metas)]
            if not qs:
                c.log("  no evaluation questions apply to this corpus: gate skipped")
                return {"eval_questions": 0, "eval_hit_rate": None}
            vecs = [v for i in range(0, len(qs), BATCH) for v in self._embed(c, [q["question"] for q in qs[i:i + BATCH]])]
            r = _retrieval()
            hits = 0
            with self._conn(c) as cx:
                for q, v in zip(qs, vecs):
                    res = r.search(cx, schema, v, q["question"], int(c.params["top_k"]))
                    hits += 1 if r.is_hit(res, q["expect"]) else 0
            rate = round(hits / len(qs), 3)
            c.log(f"  retrieval hit rate {rate} on {len(qs)} questions")
            floor = float(c.params["min_hit_rate"])
            if rate < floor:
                raise PackError(f"retrieval hit rate {rate} is below min_hit_rate {floor} on {len(qs)} questions: "
                                "not deploying. Inspect chunking/sections, or lower --param min_hit_rate")
            return {"eval_questions": len(qs), "eval_hit_rate": rate}

        def app(c: PackContext):
            name = f"rag-{c.name}"
            res = [{"name": "embedder", "serving_endpoint": {"name": c.state["embedding_endpoint"], "permission": "CAN_QUERY"}},
                   {"name": "chat", "serving_endpoint": {"name": c.state["chat_endpoint"], "permission": "CAN_QUERY"}}]
            if not d().exists("apps", "get", name):
                d().json("apps", "create", body={"name": name, "description": f"labagent RAG ({self.source.id}) on Lakebase", "resources": res})
            sp = pgmod.find(d().json("apps", "get", name), "service_principal_client_id")
            if not sp:
                raise PackError("app service principal not reported yet; re-run")
            branch = c.state["lakebase_endpoint"].rsplit("/endpoints/", 1)[0]
            try:
                d().json("postgres", "create-role", branch, "--role-id", re.sub(r"[^a-z0-9-]", "-", name.lower()),
                         body={"spec": {"postgres_role": sp, "identity_type": "SERVICE_PRINCIPAL", "auth_method": "LAKEBASE_OAUTH_V1"}})
            except PackError as e:
                if "already exists" not in str(e).lower():
                    raise
            with self._conn(c) as cx:
                cx.execute(f'GRANT USAGE ON SCHEMA {schema} TO "{sp}"')
                cx.execute(f'GRANT SELECT ON {schema}.chunks TO "{sp}"')
            with tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / "app"
                shutil.copytree(HERE / "app", bundle)
                y = (bundle / "app.yaml").read_text()
                for k, v in {"PGHOST": c.state["pg_host"], "LAKEBASE_ENDPOINT": c.state["lakebase_endpoint"],
                             "EMBED_ENDPOINT": c.state["embedding_endpoint"], "CHAT_ENDPOINT": c.state["chat_endpoint"],
                             "RAG_SCHEMA": schema, "TOP_K": str(int(c.params["top_k"]))}.items():
                    y = y.replace(f"__{k}__", v)
                (bundle / "app.yaml").write_text(y)
                (bundle / "ui.json").write_text(json.dumps(self.source.ui([x["meta"] for x in self._read_chunks(c)]), indent=1))
                path = f"/Users/{c.state['user']}/labagent/{name}"
                d().text("workspace", "import-dir", str(bundle), path, "--overwrite")
            d().text("apps", "deploy", name, "--source-code-path", f"/Workspace{path}")
            return {"app_name": name, "app_path": path, "app_url": pgmod.find(d().json("apps", "get", name), "url")}

        return [
            Step("preflight", "Check login, serving endpoints and Free Edition project limit", preflight),
            Step("project", "Create the Lakebase project", mk_project, check=lambda c: d().exists("postgres", "get-project", f"projects/{project}")),
            Step("locate", "Find the compute endpoint", locate),
            Step("build", f"Fetch, parse and chunk the '{self.source.id}' corpus", build, check=lambda c: self._chunks_file(c).exists() and "chunks" in c.state),
            Step("schema", "Enable pgvector and create the table", sch),
            Step("load", "Embed the chunks and load them", load, check=loaded),
            Step("index", "Build vector, full-text and metadata indexes", index),
            Step("evaluate", "Score retrieval on the evaluation questions (deploy gate)", evaluate),
            Step("app", "Deploy the Databricks App", app),
        ]

    # -- destroy -------------------------------------------------------------
    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731
        name = f"rag-{ctx.name}"
        project = self._project(ctx)

        def rm_schema(c: PackContext):
            if c.state.get("project_created"):
                return          # the project delete below removes it
            try:
                with self._conn(c) as cx:
                    cx.execute(f"DROP SCHEMA IF EXISTS {self.source.schema} CASCADE")
            except Exception as e:
                c.log(f"could not drop schema ({type(e).__name__}); remove it manually if the project still exists")

        def rm_project(c: PackContext):
            if not c.state.get("project_created"):
                c.log("project was not created by labagent: leaving it in place")
                return
            d().json("postgres", "delete-project", f"projects/{project}")

        def rm_app(c: PackContext):
            try:
                d().text("apps", "delete", name)
            except PackError as e:
                if "DELETING" not in str(e):     # a delete already in flight is success
                    raise

        def rm_code(c: PackContext):
            d().text("workspace", "delete", c.state["app_path"], "--recursive")
            c.state.pop("app_path", None)
            return {}

        return [
            Step("app", "Delete the Databricks App", rm_app, check=lambda c: not d().exists("apps", "get", name)),
            Step("code", "Delete uploaded app code", rm_code, check=lambda c: "app_path" not in c.state),
            Step("schema", "Drop this pack's schema (only when the project is reused)", rm_schema),
            Step("project", "Delete the Lakebase project (only if labagent created it)", rm_project),
        ]


class RagLakebase(RagPack):
    id = "rag-lakebase"
    title = "RAG demo on Lakebase (pgvector)"
    summary = "Demo corpus (25 invented passages) to smoke-test the RAG pipeline and Databricks setup."
    source_id = "builtin"


class RagSecFilings(RagPack):
    id = "rag-sec-filings"
    title = "Financial research RAG over SEC 10-K filings"
    summary = "RAG over a pinned snapshot of real 10-Ks: hybrid search, metadata filters, citations, eval gate."
    source_id = "sec-10k-snapshot"


class RagSecFilingsLive(RagPack):
    id = "rag-sec-filings-live"
    title = "Financial research RAG, fresh from SEC EDGAR"
    summary = "Same pipeline, but downloads the filings from EDGAR at launch (needs a contact for the SEC)."
    source_id = "sec-10k"
