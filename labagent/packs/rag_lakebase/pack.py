"""rag-lakebase: RAG app on Databricks Lakebase (Postgres + pgvector), launched into the learner's own workspace.

Runs on the learner's machine with their Databricks CLI profile. labagent never receives their credentials.
Steps are idempotent: re-run `launch` to resume, `destroy` removes only what this pack created.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Pack, PackContext, PackError, Param, Step
from ..databricks.cli import Dbx
from ..databricks import pg as pgmod

HERE = Path(__file__).parent
EMBED_RE = re.compile(r"gte|bge|embed", re.I)
CHAT_RE = re.compile(r"llama|gpt|claude|gemma|qwen|instruct|chat", re.I)
BATCH = 16


def corpus() -> List[Dict[str, Any]]:
    return [json.loads(l) for l in (HERE / "corpus.jsonl").read_text().splitlines() if l.strip()]


def _items(resp: Any, key: str) -> List[Dict[str, Any]]:
    if isinstance(resp, list):
        return resp
    return (resp or {}).get(key) or []


def _vec(v: List[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


class RagLakebase(Pack):
    id = "rag-lakebase"
    title = "RAG app on Lakebase (pgvector)"
    summary = "Postgres+pgvector on Databricks Lakebase, pre-loaded knowledge base, deployed as a Databricks App."
    output_keys = ("app_url", "project", "embedding_endpoint", "chat_endpoint", "documents")
    params = [
        Param("profile", None, "Databricks CLI profile (default profile if omitted)"),
        Param("project", None, "Lakebase project id (default: rag-<name>). Free Edition allows ONE project per account"),
        Param("embedding_endpoint", None, "Serving endpoint for embeddings (auto-discovered if omitted)"),
        Param("chat_endpoint", None, "Serving endpoint for answers (auto-discovered if omitted)"),
        Param("top_k", 4, "Passages retrieved per question"),
    ]

    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {"dbx": Dbx(profile), "pg": pgmod.connect}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _project(ctx: PackContext) -> str:
        return ctx.params["project"] or f"rag-{ctx.name}"

    @staticmethod
    def _embed(ctx: PackContext, texts: List[str]) -> List[List[float]]:
        r = ctx.services["dbx"].api("post", f"/serving-endpoints/{ctx.state['embedding_endpoint']}/invocations", {"input": texts})
        return [d["embedding"] for d in r["data"]]

    @staticmethod
    def _conn(ctx: PackContext):
        d = ctx.services["dbx"]
        tok = pgmod.find(d.json("postgres", "generate-database-credential", ctx.state["lakebase_endpoint"]), "token")
        if not tok:
            raise PackError("could not obtain a Lakebase credential: run `databricks auth login` and retry")
        return ctx.services["pg"](ctx.state["pg_host"], ctx.state["user"], tok)

    # -- deploy --------------------------------------------------------------
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731
        project = self._project(ctx)

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
            parent = f"projects/{project}"
            branches = _items(d().json("postgres", "list-branches", parent), "branches")
            if not branches:
                raise PackError("project has no branch yet; wait a minute and re-run")
            br = next((b for b in branches if pgmod.find(b, "default", "is_default") is True), branches[0])
            eps = _items(d().json("postgres", "list-endpoints", br["name"]), "endpoints")
            if not eps:
                raise PackError("project has no compute endpoint yet; wait a minute and re-run")
            host = pgmod.find(eps[0], "host")
            if not host:
                raise PackError("endpoint host not reported yet (compute may be starting); re-run in a minute")
            return {"lakebase_endpoint": eps[0]["name"], "pg_host": host}

        def schema(c: PackContext):
            dim = len(self._embed(c, ["dimension probe"])[0])
            with self._conn(c) as cx:
                cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cx.execute("CREATE SCHEMA IF NOT EXISTS rag")
                cx.execute(f"CREATE TABLE IF NOT EXISTS rag.chunks (id int PRIMARY KEY, source text, title text, "
                           f"content text, embedding vector({int(dim)}))")
            return {"dim": dim}

        def load(c: PackContext):
            docs = corpus()
            for i in range(0, len(docs), BATCH):
                batch = docs[i:i + BATCH]
                vecs = self._embed(c, [f"{x['title']}. {x['content']}" for x in batch])
                with self._conn(c) as cx:
                    for x, v in zip(batch, vecs):
                        cx.execute("INSERT INTO rag.chunks VALUES (%s,%s,%s,%s,%s::vector) ON CONFLICT (id) DO UPDATE SET "
                                   "source=EXCLUDED.source,title=EXCLUDED.title,content=EXCLUDED.content,embedding=EXCLUDED.embedding",
                                   (x["id"], x["source"], x["title"], x["content"], _vec(v)))
            return {"documents": len(docs)}

        def loaded(c: PackContext) -> bool:
            try:
                with self._conn(c) as cx:
                    return cx.execute("SELECT count(*) FROM rag.chunks").fetchone()[0] == len(corpus())
            except Exception:
                return False

        def index(c: PackContext):
            with self._conn(c) as cx:
                cx.execute("CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON rag.chunks USING hnsw (embedding vector_cosine_ops)")

        def app(c: PackContext):
            name = f"rag-{c.name}"
            res = [{"name": "embedder", "serving_endpoint": {"name": c.state["embedding_endpoint"], "permission": "CAN_QUERY"}},
                   {"name": "chat", "serving_endpoint": {"name": c.state["chat_endpoint"], "permission": "CAN_QUERY"}}]
            if not d().exists("apps", "get", name):
                d().json("apps", "create", name, body={"name": name, "description": "labagent RAG on Lakebase", "resources": res})
            info = d().json("apps", "get", name)
            sp = pgmod.find(info, "service_principal_client_id")
            if not sp:
                raise PackError("app service principal not reported yet; re-run")
            branch = c.state["lakebase_endpoint"].rsplit("/endpoints/", 1)[0]
            try:
                d().json("postgres", "create-role", branch, body={"spec": {"postgres_role": sp, "identity_type": "SERVICE_PRINCIPAL",
                                                                           "auth_method": "LAKEBASE_OAUTH_V1"}})
            except PackError as e:
                if "already exists" not in str(e).lower():
                    raise
            with self._conn(c) as cx:
                cx.execute(f'GRANT USAGE ON SCHEMA rag TO "{sp}"')
                cx.execute(f'GRANT SELECT ON rag.chunks TO "{sp}"')
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "app"
                shutil.copytree(HERE / "app", src)
                y = (src / "app.yaml").read_text()
                for k, v in {"PGHOST": c.state["pg_host"], "LAKEBASE_ENDPOINT": c.state["lakebase_endpoint"],
                             "EMBED_ENDPOINT": c.state["embedding_endpoint"], "CHAT_ENDPOINT": c.state["chat_endpoint"],
                             "TOP_K": str(int(c.params["top_k"]))}.items():
                    y = y.replace(f"__{k}__", v)
                (src / "app.yaml").write_text(y)
                path = f"/Users/{c.state['user']}/labagent/{name}"
                d().text("workspace", "import-dir", str(src), path, "--overwrite")
            d().text("apps", "deploy", name, "--source-code-path", path)
            return {"app_name": name, "app_path": path, "app_url": pgmod.find(d().json("apps", "get", name), "url")}

        return [
            Step("preflight", "Check login, serving endpoints and Free Edition project limit", preflight),
            Step("project", "Create the Lakebase project", mk_project,
                 check=lambda c: d().exists("postgres", "get-project", f"projects/{project}")),
            Step("locate", "Find the compute endpoint", locate),
            Step("schema", "Enable pgvector and create the table", schema),
            Step("load", f"Embed and load {len(corpus())} knowledge-base passages", load, check=loaded),
            Step("index", "Build the HNSW index", index),
            Step("app", "Deploy the Databricks App", app),
        ]

    # -- destroy -------------------------------------------------------------
    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731
        name = f"rag-{ctx.name}"
        project = self._project(ctx)

        def rm_app(c: PackContext):
            d().text("apps", "delete", name)

        def rm_code(c: PackContext):
            d().text("workspace", "delete", c.state["app_path"], "--recursive")

        def rm_project(c: PackContext):
            if not c.state.get("project_created"):
                c.log("project was not created by labagent: leaving it in place")
                return
            d().json("postgres", "delete-project", f"projects/{project}")

        return [
            Step("app", "Delete the Databricks App", rm_app, check=lambda c: not d().exists("apps", "get", name)),
            Step("code", "Delete uploaded app code", rm_code, check=lambda c: "app_path" not in c.state),
            Step("project", "Delete the Lakebase project (only if labagent created it)", rm_project),
        ]
