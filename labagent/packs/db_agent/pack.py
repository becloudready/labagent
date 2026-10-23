"""db-agent: deploy the open-source text-to-SQL agent (https://github.com/db-agent/db-agent) to Databricks Apps.

labagent vendors none of db-agent's code. It clones the repo at a pinned commit, writes an app.yaml for the learner's own
workspace, binds a revocable token as an app secret, deploys, and smoke-tests a real question end to end.
The data is the `samples` catalog every workspace already has (default: samples.bakehouse), so nothing is downloaded.
Runs on the learner's machine with their own Databricks CLI profile.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..base import Pack, PackContext, PackError, Param, Step, state_dir
from ..databricks import pg as pgmod
from ..databricks.auth import login, workspace_host
from ..databricks.cli import Dbx
from ..gitsrc import clone_pinned
from ..rag.pack import CHAT_RE, EMBED_RE

REPO = "https://github.com/db-agent/db-agent"
PINNED = "eef0a308d052b6bdd00fdfec7ee1c9a61c01426f"        # reviewed commit; change it deliberately (see docs/packs.md)
SECRET_KEY = "token"
SMOKE_QUESTION = "How many customers are there?"
IGNORE = shutil.ignore_patterns("node_modules", "dist", ".env", ".env.*", "knowledge.json", "*.log", "__pycache__")

APP_YAML = """command: ["npm", "run", "start"]
env:
  - name: SQL_ENGINE
    value: "databricks-sql"
  - name: DATABRICKS_HOST
    value: "{host}"
  - name: DATABRICKS_HTTP_PATH
    value: "{http_path}"
  - name: DATABRICKS_TOKEN
    valueFrom: "token"
  - name: DATABRICKS_CATALOG
    value: "{catalog}"
  - name: DATABRICKS_SCHEMA
    value: "{schema}"
  - name: LLM_BASE_URL
    value: "{host}/serving-endpoints"
  - name: LLM_MODEL
    value: "{chat}"
  - name: LLM_API_KEY
    valueFrom: "token"
  - name: EMBEDDING_MODEL
    value: "{embed}"
  - name: EMBEDDING_API_KEY
    valueFrom: "token"
  - name: DBAGENT_ID
    value: "{agent_id}"
"""


def http_json(url: str, bearer: str, body: Optional[dict] = None, timeout: int = 240) -> Tuple[int, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET",
                                 headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": f"HTTP {e.code}"}


def _items(resp: Any, key: str) -> List[Dict[str, Any]]:
    return resp if isinstance(resp, list) else (resp or {}).get(key) or []


class DbAgent(Pack):
    id = "db-agent"
    aliases = ("dbagent", "db-agent-databricks", "text-to-sql")
    default_name = "demo"
    title = "DB Agent: text-to-SQL on Databricks"
    summary = "Deploy db-agent (github.com/db-agent/db-agent) to Databricks Apps over a Databricks sample dataset, then smoke-test it."
    output_keys = ("app_url", "source", "dataset", "chat_endpoint", "smoke_status")
    params = [
        Param("profile", None, "Databricks CLI profile (default profile if omitted)"),
        Param("repo", REPO, "Git repo with the app (https only)"),
        Param("ref", PINNED, "Full commit SHA to deploy: the pin that guarantees the code you reviewed"),
        Param("catalog", "samples", "Unity Catalog catalog holding the data"),
        Param("dataset", "bakehouse", "Schema to query, e.g. bakehouse, tpch, nyctaxi, tpcds_sf1"),
        Param("warehouse", None, "SQL warehouse id or name (first one if omitted)"),
        Param("chat_endpoint", None, "Serving endpoint for the LLM (auto-discovered if omitted)"),
        Param("embedding_endpoint", None, "Serving endpoint for embeddings (auto-discovered if omitted)"),
        Param("token_days", 30, "Lifetime of the app's access token, in days"),
    ]

    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {"dbx": Dbx(profile), "http": http_json, "sleep": time.sleep}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _app(ctx: PackContext) -> str:
        return f"db-agent-{ctx.name}"

    @staticmethod
    def _scope(ctx: PackContext) -> str:
        return f"labagent-db-agent-{ctx.name}"

    @staticmethod
    def _checkout(ctx: PackContext) -> Path:
        return state_dir().parent / "cache" / "repos" / f"db-agent-{ctx.params['ref'][:12]}"

    # -- deploy --------------------------------------------------------------
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731
        app_name, scope = self._app(ctx), self._scope(ctx)

        def preflight(c: PackContext):
            user = login(c).get("userName")
            if not user:
                raise PackError("not authenticated: run `databricks auth login` and retry")
            host = workspace_host(c)
            whs = _items(d().json("warehouses", "list"), "warehouses")
            want = c.params["warehouse"]
            wh = next((w for w in whs if want in (w.get("id"), w.get("name"))), None) if want else (whs[0] if whs else None)
            if not wh:
                raise PackError("no SQL warehouse found" + (f" matching '{want}'" if want else "") + ": create one (Free Edition includes one 2X-Small)")
            schemas = [s.get("name") for s in _items(d().json("schemas", "list", c.params["catalog"]), "schemas")]
            if c.params["dataset"] not in schemas:
                raise PackError(f"schema '{c.params['catalog']}.{c.params['dataset']}' not found. Available: {', '.join(sorted(schemas)[:12]) or 'none'}")
            names = [e.get("name", "") for e in _items(d().json("serving-endpoints", "list"), "endpoints")]
            chat = c.params["chat_endpoint"] or next((n for n in names if CHAT_RE.search(n) and not EMBED_RE.search(n)), None)
            emb = c.params["embedding_endpoint"] or next((n for n in names if EMBED_RE.search(n)), None)
            if not chat or not emb:
                raise PackError("no chat/embedding serving endpoint found. Pass --param chat_endpoint=... --param embedding_endpoint=... "
                                f"(available: {', '.join(names[:8]) or 'none'})")
            return {"user": user, "host": host, "warehouse_id": wh["id"], "chat_endpoint": chat, "embedding_endpoint": emb,
                    "dataset": f"{c.params['catalog']}.{c.params['dataset']}"}

        def fetch(c: PackContext):
            clone_pinned(c.params["repo"], c.params["ref"], self._checkout(c))
            if not (self._checkout(c) / "app" / "server.js").exists():
                raise PackError("this repo/commit has no app/server.js: not a db-agent checkout")
            return {"source": f"{c.params['repo']}@{c.params['ref'][:12]}", "source_sha": c.params["ref"]}

        def token(c: PackContext):
            """A revocable token, stored only as an app secret (never in state, logs or the result)."""
            scopes = [s.get("name") for s in _items(d().json("secrets", "list-scopes"), "scopes")]
            created_scope = scope not in scopes
            if created_scope:
                d().text("secrets", "create-scope", scope)
            value, token_id = os.environ.get("LABAGENT_DATABRICKS_TOKEN"), None
            if not value:
                try:
                    r = d().json("tokens", "create", "--lifetime-seconds", str(int(c.params["token_days"]) * 86400),
                                 "--comment", f"labagent {app_name}")
                except PackError as e:
                    raise PackError(f"could not create an access token ({e}). Create one yourself and export it as "
                                    "LABAGENT_DATABRICKS_TOKEN, then re-run") from e
                value, token_id = r.get("token_value"), pgmod.find(r, "token_id")
            if not value:
                raise PackError("the workspace did not return a token value")
            try:
                d().text("secrets", "put-secret", scope, SECRET_KEY, stdin=value)
            except PackError:
                if token_id:
                    try:
                        d().text("tokens", "delete", token_id)     # do not leave an orphaned credential behind
                    except PackError:
                        pass
                raise
            return {"secret_scope": scope, "scope_created": created_scope, "token_id": token_id}

        def token_ready(c: PackContext) -> bool:
            if c.state.get("secret_scope") != scope:
                return False
            keys = [s.get("key") for s in _items(d().json("secrets", "list-secrets", scope), "secrets")]
            return SECRET_KEY in keys

        def app(c: PackContext):
            resources = [{"name": "token", "secret": {"scope": scope, "key": SECRET_KEY, "permission": "READ"}}]
            desc = f"DB Agent ({c.params['repo']}) deployed by labagent"
            if not d().exists("apps", "get", app_name):
                d().json("apps", "create", body={"name": app_name, "description": desc, "resources": resources})
            else:
                d().json("apps", "update", app_name, body={"description": desc, "resources": resources})
            schema_name = c.params["dataset"]
            with tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / "app"
                shutil.copytree(self._checkout(c) / "app", bundle, ignore=IGNORE)
                (bundle / "app.yaml").write_text(APP_YAML.format(
                    host=c.state["host"], http_path=f"/sql/1.0/warehouses/{c.state['warehouse_id']}", catalog=c.params["catalog"],
                    schema=schema_name, chat=c.state["chat_endpoint"], embed=c.state["embedding_endpoint"], agent_id=app_name))
                path = f"/Users/{c.state['user']}/labagent/{app_name}"
                d().text("workspace", "import-dir", str(bundle), path, "--overwrite")
            d().text("apps", "deploy", app_name, "--source-code-path", f"/Workspace{path}")
            return {"app_name": app_name, "app_path": path, "app_url": pgmod.find(d().json("apps", "get", app_name), "url")}

        def verify(c: PackContext):
            """Ask a real question through the deployed app: proves token, warehouse, LLM and the safety layer work together."""
            http, sleep = c.services["http"], c.services.get("sleep", time.sleep)
            deadline = time.monotonic() + 600
            while True:
                st = ((d().json("apps", "get", app_name) or {}).get("app_status") or {}).get("state")   # exact field, not a search
                if st == "RUNNING" or time.monotonic() > deadline:
                    break
                sleep(10)
            if st != "RUNNING":
                raise PackError(f"app is not running (state: {st}). Check: databricks apps logs {app_name}")
            bearer = pgmod.find(d().json("auth", "token"), "access_token")
            if not bearer:
                raise PackError("could not obtain a login token to call the app: run `databricks auth login`")
            url = str(c.state["app_url"]).rstrip("/")
            last = ""
            for attempt in range(4):             # the first query can wait for the warehouse to start
                code, body = http(f"{url}/api/ask", bearer, {"question": SMOKE_QUESTION})
                status = (body or {}).get("status")
                if code == 200 and status in ("complete", "awaiting_approval") and (body.get("sql") or body.get("rows") is not None):
                    return {"smoke_status": status}
                last = f"HTTP {code} {(body or {}).get('error', status)}"
                sleep(15)
            raise PackError(f"the deployed app did not answer a test question ({str(last)[:160]}). Check: databricks apps logs {app_name}")

        return [
            Step("preflight", "Check login, warehouse, sample data and serving endpoints", preflight),
            Step("fetch", f"Fetch db-agent at the pinned commit ({str(ctx.params['ref'])[:12]})", fetch,
                 check=lambda c: c.state.get("source_sha") == c.params["ref"] and (self._checkout(c) / "app" / "server.js").exists()),
            Step("token", "Create a revocable app token and store it as a secret", token, check=token_ready),
            Step("app", "Deploy the Databricks App", app),
            Step("verify", "Ask a test question through the deployed app", verify),
        ]

    # -- destroy -------------------------------------------------------------
    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731
        app_name = self._app(ctx)

        def rm_app(c: PackContext):
            try:
                d().text("apps", "delete", app_name)
            except PackError as e:
                if "DELETING" not in str(e):
                    raise

        def rm_code(c: PackContext):
            d().text("workspace", "delete", c.state["app_path"], "--recursive")
            c.state.pop("app_path", None)
            return {}

        def rm_token(c: PackContext):
            if c.state.get("token_id"):
                try:
                    d().text("tokens", "delete", c.state["token_id"])
                except PackError as e:
                    if "RESOURCE_DOES_NOT_EXIST" not in str(e):
                        raise
                c.state.pop("token_id", None)
            if c.state.get("scope_created") and c.state.get("secret_scope"):
                try:
                    d().text("secrets", "delete-scope", c.state["secret_scope"])
                except PackError as e:
                    if "RESOURCE_DOES_NOT_EXIST" not in str(e):
                        raise
            return {}

        return [
            Step("app", "Delete the Databricks App", rm_app, check=lambda c: not d().exists("apps", "get", app_name)),
            Step("code", "Delete uploaded app code", rm_code, check=lambda c: "app_path" not in c.state),
            Step("token", "Revoke the app token and delete its secret scope", rm_token),
        ]
