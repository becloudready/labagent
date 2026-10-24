"""rag-sec-tutorial: the SEC filings RAG lab, as notebooks a student runs step by step.

This pack builds nothing itself. It uploads nine notebooks (setup, data, embeddings, pgvector,
search, answer and evaluate, deploy the app, clean up, plus one shared helper) and the app code into your Databricks workspace.
The student then runs them in order and learns what each step does. Notebook 06 deploys the finished app from the code uploaded next to the notebooks.

Nothing is created in Databricks except the uploaded folder. The notebooks create and remove the database and the app themselves.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ... import datasets
from ..base import Pack, PackContext, PackError, Param, Step
from ..databricks.auth import login as login_shared
from ..databricks.auth import workspace_host
from ..databricks.cli import Dbx
from ..databricks.models import CHAT_RE, EMBED_RE
from . import sources as src_mod

HERE = Path(__file__).parent
NOTEBOOKS = HERE / "tutorial" / "notebooks"
APP = HERE / "app"
DATASET = "sec-10k-2026.10"
SOURCE_ID = "sec-10k-snapshot"


def _items(resp: Any, key: str) -> List[Dict[str, Any]]:
    if isinstance(resp, list):
        return resp
    return (resp or {}).get(key) or []


def notebook_text(name: str, replacements: Dict[str, str]) -> str:
    text = (NOTEBOOKS / name).read_text()
    for key, value in replacements.items():
        text = text.replace(f"__{key}__", value)
    return text


class RagSecTutorial(Pack):
    id = "rag-sec-tutorial"
    title = "RAG on Databricks, step by step (notebooks)"
    summary = "Uploads nine notebooks and the app code to your workspace so a student builds the SEC filings RAG one step at a time."
    aliases = ("rag-tutorial", "sec-tutorial", "rag-notebooks")
    default_name = "demo"
    output_keys = ("workspace_path", "url", "notebooks")
    params = [
        Param("profile", None, "Databricks CLI profile (default profile if omitted)"),
        Param("dataset", DATASET, "Pinned dataset snapshot the notebooks download"),
    ]

    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {"dbx": Dbx(profile)}

    @staticmethod
    def folder(ctx: PackContext) -> str:
        return f"/Users/{ctx.state['user']}/labagent-rag-tutorial-{ctx.name}"

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731

        def preflight(c: PackContext):
            me = login_shared(c)
            user = me.get("userName")
            if not user:
                raise PackError("not authenticated: run `databricks auth login` and retry")
            names = [e.get("name", "") for e in _items(d().json("serving-endpoints", "list"), "endpoints")]
            if not any(EMBED_RE.search(n) for n in names) or not any(CHAT_RE.search(n) and not EMBED_RE.search(n) for n in names):
                raise PackError("this workspace has no embedding or chat model serving endpoint, which the tutorial needs "
                                f"(found: {', '.join(names[:8]) or 'none'})")
            if c.params["dataset"] not in datasets.registry():
                raise PackError(f"unknown dataset '{c.params['dataset']}'. Known: {', '.join(sorted(datasets.registry()))}")
            return {"user": user}

        def upload(c: PackContext):
            path = self.folder(c)
            entry = datasets.registry()[c.params["dataset"]]
            rows = datasets.fetch_snapshot(c.params["dataset"], download=c.services.get("download", datasets.default_http))
            ui = src_mod.get(SOURCE_ID).ui([r["meta"] for r in rows])
            repl = {"SNAPSHOT_URL": entry["url"], "SNAPSHOT_SHA256": entry["sha256"], "WORKSPACE_FOLDER": f"/Workspace{path}"}
            with tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / "bundle"
                bundle.mkdir()
                for nb in sorted(NOTEBOOKS.glob("*.py")):
                    (bundle / nb.name).write_text(notebook_text(nb.name, repl))
                shutil.copytree(APP, bundle / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                (bundle / "app" / "ui.json").write_text(json.dumps(ui, indent=1))
                d().text("workspace", "mkdirs", path)
                d().text("workspace", "import-dir", str(bundle), path, "--overwrite")
            return {"workspace_path": path, "notebooks": len(list(NOTEBOOKS.glob("*.py")))}

        def verify(c: PackContext):
            path = c.state["workspace_path"]
            listing = _items(d().json("workspace", "list", path), "objects")
            names = {Path(o.get("path", "")).name for o in listing if o.get("object_type") == "NOTEBOOK"}
            missing = sorted({p.stem for p in NOTEBOOKS.glob("*.py")} - names)
            if missing:
                raise PackError(f"these notebooks did not upload: {', '.join(missing)}")
            status = d().json("workspace", "get-status", path)
            url = f"{workspace_host(c)}/browse/folders/{status.get('object_id')}"
            c.log(f"\n  Open: {url}\n  Start with 00_setup and run the notebooks in order. Finish with 99_cleanup.")
            return {"url": url}

        return [Step("preflight", "Check login and the model endpoints the notebooks use", preflight),
                Step("upload", "Upload the notebooks and the app code", upload),
                Step("verify", "Check that every notebook arrived", verify)]

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731

        def remove(c: PackContext):
            if not c.state.get("workspace_path"):
                return {}
            c.log("  reminder: this removes only the notebooks. Run 99_cleanup first to remove the database and the app you created.")
            d().text("workspace", "delete", c.state["workspace_path"], "--recursive")
            return {"workspace_path": None, "url": None}

        return [Step("login", "Check login", lambda c: {"user": login_shared(c).get("userName")}),
                Step("folder", "Delete the uploaded tutorial folder", remove)]
