"""dbx-lakehouse: a ready-to-teach medallion lakehouse in a Databricks workspace.

For each domain (retail, healthcare) the pack creates bronze, silver and gold schemas in Unity Catalog, uploads real open data to a volume,
builds every layer with SQL, checks the result, applies governance (tags, column masks, a row filter) and shares the tables with learners.
It also uploads the SQL as notebooks, with exercises, so a learner can read and re-run each step.

It uses only a SQL warehouse and the Databricks CLI login, so the same pack runs on Databricks Free Edition, AWS and Azure. No cluster, job or Terraform.
Nothing here is a secret: the CLI holds the login, and the pack never sees a token.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ... import datasets
from ..base import Pack, PackContext, PackError, Param, Step, save_state
from ..databricks.auth import login as login_shared
from ..databricks.auth import workspace_host
from ..databricks.cli import Dbx
from . import notebook
from .domains import DOMAINS, LAYERS, Domain, landing_folder
from .sqlrun import Sql

HERE = Path(__file__).parent
SQL_DIR = HERE / "sql"
GUIDES = HERE / "guides"
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PRINCIPAL = re.compile(r"^[A-Za-z0-9][\w .@-]{0,127}$")      # a group or user name: no quotes, no backticks
NOT_FOR_UPLOAD = {"provenance.json"}                         # the licence notice goes to the volume with the data; the row counts stay local


def _items(resp: Any, key: str) -> List[Dict[str, Any]]:
    if isinstance(resp, list):
        return resp
    return (resp or {}).get(key) or []


def _short(err: Exception, limit: int = 160) -> str:
    return " ".join(str(err).split())[:limit]


class DatabricksLakehouse(Pack):
    id = "dbx-lakehouse"
    title = "Medallion lakehouse with real data on Databricks (Free Edition, AWS or Azure)"
    summary = ("Builds bronze, silver and gold tables in Unity Catalog from real open data (online retail, synthetic healthcare), "
               "with governance applied and exercises, ready to teach")
    aliases = ("lakehouse", "medallion", "databricks-lakehouse")
    platforms = ("databricks",)
    cost_tier = "low"
    default_name = "lakehouse"
    output_keys = ("catalog", "workspace_path", "url", "warehouse_id")
    params = [
        Param("profile", None, "Databricks CLI profile (default profile if omitted)"),
        Param("domains", "retail,healthcare", "Comma-separated: retail | healthcare"),
        Param("catalog", "lakehouse", "Unity Catalog catalog to create. If the workspace cannot create one, the current catalog is used"),
        Param("warehouse_id", None, "SQL warehouse to run on (default: a serverless or running one, or a new 2X-Small if there is none)"),
        Param("readers", "account users", "Who can read silver and gold and use the sandbox schema. Comma-separated groups or users. Empty: share with nobody"),
        Param("clinical_group", "lakehouse_clinicians", "Healthcare: account group that sees unmasked identifiers. Create it in the account console"),
        Param("data_dir", None, "Folder with <domain>/ subfolders holding the unpacked snapshot files. Skips the download (offline use, testing)"),
        Param("rebuild", "no", "yes: run every step again even if it already finished"),
    ]

    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {"dbx": Dbx(profile)}

    # ---- helpers -----------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _domains(raw: Any) -> List[str]:
        names: List[str] = []
        for part in str(raw or "").split(","):
            part = part.strip().lower()
            if part and part not in names:
                names.append(part)
        return names

    def _check(self, c: PackContext) -> Dict[str, Any]:
        p = c.params
        names = self._domains(p["domains"])
        unknown = [n for n in names if n not in DOMAINS]
        if not names or unknown:
            raise PackError(f"unknown domain(s): {', '.join(unknown) or '(none given)'}. Choose from: {', '.join(DOMAINS)}")
        cat = str(p["catalog"] or "")
        if not IDENT.match(cat):
            raise PackError("catalog must be lowercase letters, digits and underscores, starting with a letter")
        group = str(p["clinical_group"] or "")
        if not PRINCIPAL.match(group):
            raise PackError("clinical_group has characters that are not allowed in a group name")
        readers = [r.strip() for r in str(p["readers"] or "").split(",") if r.strip()]
        bad = [r for r in readers if not PRINCIPAL.match(r)]
        if bad:
            raise PackError("readers must be group or user names without quotes or backticks. Separate several with commas")
        rebuild = str(p["rebuild"]).lower() == "yes"
        return {"domains": names, "readers": readers, "group": group, "rebuild": rebuild}

    @staticmethod
    def folder(c: PackContext) -> str:
        return f"/Users/{c.state['user']}/labagent-lakehouse-{c.name}"

    @staticmethod
    def _remember(c: PackContext, **kv: Any) -> None:
        """Persist right away. A step that fails half way must not forget what it already created."""
        c.state.update(kv)
        save_state(c.pack_id, c.name, c.state)

    @staticmethod
    def _merge_domain(c: PackContext, dom: str, **kv: Any) -> Dict[str, Any]:
        domains = dict(c.state.get("domains") or {})
        domains[dom] = {**domains.get(dom, {}), **kv}
        return {"domains": domains}

    def _sql(self, c: PackContext) -> Any:
        if "sql" in c.services:
            return c.services["sql"]
        if "sql" not in c.mem:
            c.mem["sql"] = Sql(c.services["dbx"], c.state["warehouse_id"])
        return c.mem["sql"]

    def _values(self, c: PackContext) -> Dict[str, str]:
        return {"CATALOG": c.state["catalog"], "CLINICAL_GROUP": self._check(c)["group"]}

    def _done(self, c: PackContext, dom: str, flag: str) -> bool:
        return bool((c.state.get("domains") or {}).get(dom, {}).get(flag)) and not self._check(c)["rebuild"]

    def _warehouse(self, c: PackContext) -> Dict[str, Any]:
        d = c.services["dbx"]
        usable = [w for w in _items(d.json("warehouses", "list"), "warehouses") if w.get("state") not in ("DELETED", "DELETING")]
        ids = {w.get("id") for w in usable}
        want = c.params["warehouse_id"] or (c.state.get("warehouse_id") if c.state.get("warehouse_id") in ids else None)
        if want:
            if want not in ids:
                raise PackError(f"warehouse {want} was not found in this workspace")
            return {"warehouse_id": want}
        if usable:
            best = sorted(usable, key=lambda w: (not w.get("enable_serverless_compute"), w.get("state") != "RUNNING"))[0]
            return {"warehouse_id": best["id"]}
        c.log("  no SQL warehouse found: creating a 2X-Small one that stops after 10 idle minutes")
        body = {"name": "labagent-lakehouse", "cluster_size": "2X-Small", "min_num_clusters": 1, "max_num_clusters": 1, "auto_stop_mins": 10,
                "enable_serverless_compute": True, "warehouse_type": "PRO"}
        try:
            made = d.json("warehouses", "create", body=body)
        except PackError as e:
            raise PackError(f"this workspace has no SQL warehouse and one could not be created ({_short(e)}). "
                            "Create one in the SQL Warehouses page, then pass --param warehouse_id=<id>") from e
        if not made.get("id"):
            raise PackError("the warehouse was created but its id was not returned: pass --param warehouse_id=<id>")
        return {"warehouse_id": made["id"], "warehouse_created": True}

    def _ensure_schema(self, c: PackContext, name: str, comment: str) -> None:
        sql, cat = self._sql(c), c.state["catalog"]
        mine = list(c.state.get("schemas") or [])
        if name not in mine and sql.run(f"SHOW SCHEMAS IN {cat} LIKE '{name}'"):
            raise PackError(f"the schema {cat}.{name} already exists and was not created by this pack. "
                            f"Use another catalog (--param catalog=...) or drop that schema first")
        sql.run(f"CREATE SCHEMA IF NOT EXISTS {cat}.{name} COMMENT '{comment}'")
        if name not in mine:
            self._remember(c, schemas=[*mine, name])

    def _snapshot(self, c: PackContext, dom: Domain) -> Path:
        given = c.params["data_dir"]
        if given:
            folder = Path(str(given)).expanduser() / dom.id
            if not (folder / "provenance.json").exists():
                raise PackError(f"data_dir has no {dom.id}/provenance.json: unpack the snapshot into {folder}")
            return folder
        dest = Path(tempfile.mkdtemp(prefix="labagent-lakehouse-"))
        try:
            datasets.fetch_table_snapshot(dom.dataset, dest, download=c.services.get("download", datasets.default_http))
        except datasets.DatasetError as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise PackError(str(e)) from e
        return dest

    # ---- launch ------------------------------------------------------------------------------------------------------------------
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        # Parameters are validated by the first step. Here only the shape of the plan is needed, so a bad value never raises outside a step.
        base = {"domains": self._domains(ctx.params["domains"]), "rebuild": str(ctx.params["rebuild"]).lower() == "yes"}
        d = lambda: ctx.services["dbx"]  # noqa: E731
        steps: List[Step] = []

        def preflight(c: PackContext):
            self._check(c)                                  # raises a PackError the learner can act on
            user = login_shared(c).get("userName")
            if not user:
                raise PackError("not authenticated: run `databricks auth login` and retry")
            out = {"user": user, **self._warehouse(c)}
            if out.get("warehouse_created"):
                self._remember(c, **out)                    # destroy must find a warehouse this pack created, even if a later step fails
            sql = c.services["sql"] if "sql" in c.services else c.mem.setdefault("sql", Sql(c.services["dbx"], out["warehouse_id"]))
            c.log("  starting the warehouse if it is stopped (serverless takes seconds, a classic one a few minutes)")
            sql.run("SELECT 1")
            return out

        def catalog(c: PackContext):
            sql, cat = self._sql(c), str(c.params["catalog"])
            if sql.run(f"SHOW CATALOGS LIKE '{cat}'"):
                c.log(f"  catalog {cat} already exists: using it")
                used, created = cat, False
            else:
                try:
                    sql.run(f"CREATE CATALOG {cat} COMMENT 'labagent lakehouse lab: bronze, silver and gold tables from open data'")
                    used, created = cat, True
                except PackError as e:
                    current = sql.scalar("SELECT current_catalog()")
                    if not current or current in ("hive_metastore", "spark_catalog"):
                        raise PackError("this workspace has no Unity Catalog catalog the pack can use. Create one, or enable Unity Catalog") from e
                    if not IDENT.match(str(current)):
                        raise PackError(f"could not create catalog {cat} ({_short(e)}) and the current catalog {current!r} has a name the pack cannot use. "
                                        "Create a catalog with a simple name and pass --param catalog=<name>") from e
                    c.log(f"  could not create catalog {cat} ({_short(e)}): using the current catalog {current} instead")
                    used, created = str(current), False
            self._remember(c, catalog=used, catalog_created=created)
            self._ensure_schema(c, "sandbox", "Scratch space for learners")
            return {}

        steps += [Step("preflight", "Check login and find a SQL warehouse", preflight),
                  Step("catalog", "Create the catalog and the sandbox schema", catalog, check=lambda c: bool(c.state.get("catalog")) and not base["rebuild"])]

        for dom_id in [n for n in base["domains"] if n in DOMAINS]:
            dom = DOMAINS[dom_id]

            def files(c: PackContext, dom: Domain = dom):
                sql, cat = self._sql(c), c.state["catalog"]
                for layer in LAYERS:
                    self._ensure_schema(c, dom.schema(layer), f"{dom.title}: {layer}")
                sql.run(f"CREATE VOLUME IF NOT EXISTS {cat}.{dom.schema('bronze')}.landing COMMENT 'Raw files exactly as delivered'")
                folder = self._snapshot(c, dom)
                try:
                    prov = json.loads((folder / "provenance.json").read_text())
                    landing = f"/Volumes/{cat}/{dom.schema('bronze')}/landing"
                    made: set = set()
                    sent = 0
                    for f in sorted(p for p in folder.iterdir() if p.is_file() and p.name not in NOT_FOR_UPLOAD):
                        sub = landing_folder(f.name)
                        target = f"{landing}/{sub}" if sub else landing
                        if sub and sub not in made:
                            d().text("fs", "mkdir", f"dbfs:{target}")
                            made.add(sub)
                        d().text("fs", "cp", str(f), f"dbfs:{target}/{f.name}", "--overwrite")
                        sent += 1
                    c.log(f"  uploaded {sent} files to {landing}")
                finally:
                    if not c.params["data_dir"]:
                        shutil.rmtree(folder, ignore_errors=True)
                return self._merge_domain(c, dom.id, uploaded=True, expected=dom.bronze_tables(prov["info"]["rows"]))

            def build(c: PackContext, dom: Domain = dom):
                sql, values = self._sql(c), self._values(c)
                if self._check(c)["rebuild"]:
                    for t in dom.append_only:       # COPY INTO tracks files: a rebuild must start from an empty table
                        sql.run(f"DROP TABLE IF EXISTS {values['CATALOG']}.{dom.schema('bronze')}.{t}")
                skipped = []
                for nb in dom.notebooks:
                    n = 0
                    for cell in notebook.cells(notebook.substitute((SQL_DIR / dom.id / nb).read_text(), values)):
                        try:
                            sql.run(cell.sql, label=f"{dom.id}/{nb}: {cell.label}")
                            n += 1
                        except PackError as e:
                            if not cell.optional:
                                raise
                            skipped.append(f"{nb}: {cell.label}")
                            c.log(f"  skipped (optional): {cell.label} ({_short(e, 120)})")
                    c.log(f"  {dom.id}/{nb}: {n} statements")
                return self._merge_domain(c, dom.id, built=True, skipped=skipped)

            def verify(c: PackContext, dom: Domain = dom):
                sql, values = self._sql(c), self._values(c)
                cat, problems, counts = c.state["catalog"], [], {}
                for table, want in (c.state["domains"][dom.id].get("expected") or {}).items():
                    got = int(sql.scalar(f"SELECT count(*) FROM {cat}.{dom.schema('bronze')}.{table}") or 0)
                    counts[f"bronze.{table}"] = got
                    if got != want:
                        problems.append(f"bronze.{table} has {got} rows, expected {want}")
                for layer, tables in dom.silver_gold.items():
                    for table in tables:
                        got = int(sql.scalar(f"SELECT count(*) FROM {cat}.{dom.schema(layer)}.{table}") or 0)
                        counts[f"{layer}.{table}"] = got
                        if got == 0:
                            problems.append(f"{layer}.{table} is empty")
                for what, q in dom.invariants:
                    if str(sql.scalar(notebook.substitute(q, values))).lower() != "true":
                        problems.append(f"check failed: {what}")
                if problems:
                    raise PackError(f"{dom.id} was built but did not pass its checks: " + "; ".join(problems[:4]))
                c.log(f"  {dom.id}: {len(counts)} tables checked")
                return self._merge_domain(c, dom.id, verified=True, rows=counts)

            steps += [Step(f"files-{dom.id}", f"{dom.id}: create schemas and the landing volume, upload the raw files", files,
                           check=lambda c, dom=dom: self._done(c, dom.id, "uploaded")),
                      Step(f"build-{dom.id}", f"{dom.id}: build bronze, silver and gold with SQL", build,
                           check=lambda c, dom=dom: self._done(c, dom.id, "built")),
                      Step(f"verify-{dom.id}", f"{dom.id}: check row counts and invariants", verify,
                           check=lambda c, dom=dom: self._done(c, dom.id, "verified"))]
            if dom.governance:
                def govern(c: PackContext, dom: Domain = dom):
                    sql, values = self._sql(c), self._values(c)
                    skipped = []
                    for cell in notebook.cells(notebook.substitute((SQL_DIR / dom.id / dom.governance).read_text(), values)):
                        try:
                            sql.run(cell.sql, label=f"{dom.id}/{dom.governance}: {cell.label}")
                        except PackError as e:
                            if not cell.optional:
                                raise
                            skipped.append(cell.label)
                            c.log(f"  skipped (optional): {cell.label} ({_short(e, 120)})")
                    return self._merge_domain(c, dom.id, governed=True, governance_skipped=skipped)

                steps.append(Step(f"govern-{dom.id}", f"{dom.id}: apply tags, column masks and a row filter", govern,
                                  check=lambda c, dom=dom: self._done(c, dom.id, "governed")))

        def share(c: PackContext):
            sql, cat = self._sql(c), c.state["catalog"]
            readers, failed = self._check(c)["readers"], []
            if not readers:
                c.log("  readers is empty: nothing is shared")
                return {"shared": True, "grant_failures": []}
            if not c.state.get("catalog_created"):
                c.log(f"  note: {cat} was not created by this pack. Learners also need USE CATALOG on it")
            for who in readers:
                grants = []
                if c.state.get("catalog_created"):
                    grants.append(f"GRANT USE CATALOG ON CATALOG {cat} TO `{who}`")
                grants.append(f"GRANT USE SCHEMA, CREATE TABLE, CREATE VIEW, SELECT, MODIFY ON SCHEMA {cat}.sandbox TO `{who}`")
                for dom_id in self._check(c)["domains"]:
                    dom = DOMAINS[dom_id]
                    grants += [f"GRANT USE SCHEMA, SELECT ON SCHEMA {cat}.{dom.schema(layer)} TO `{who}`" for layer in dom.reader_layers]
                    if dom.volume_readable:
                        grants.append(f"GRANT READ VOLUME ON VOLUME {cat}.{dom.schema('bronze')}.landing TO `{who}`")
                for g in grants:
                    try:
                        sql.run(g)
                    except PackError as e:
                        failed.append(f"{g.split(' TO ')[0]}: {_short(e, 100)}")
                        c.log(f"  grant failed: {g.split(' TO ')[0]} ({_short(e, 100)})")
            return {"shared": True, "grant_failures": failed}

        def notebooks(c: PackContext):
            path = self.folder(c)
            values = self._values(c)
            with tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / "bundle"
                bundle.mkdir()
                (bundle / "START_HERE.md").write_text(notebook.substitute((GUIDES / "START_HERE.md").read_text(), values))
                for dom_id in self._check(c)["domains"]:
                    dom = DOMAINS[dom_id]
                    out = bundle / dom.id
                    out.mkdir()
                    for nb in (*dom.notebooks, *([dom.governance] if dom.governance else [])):
                        (out / nb).write_text(notebook.substitute((SQL_DIR / dom.id / nb).read_text(), values))
                    (out / "GUIDE.md").write_text(notebook.substitute((GUIDES / f"{dom.id}.md").read_text(), values))
                d().text("workspace", "mkdirs", path)
                d().text("workspace", "import-dir", str(bundle), path, "--overwrite")
            status = d().json("workspace", "get-status", path)
            url = f"{workspace_host(c)}/browse/folders/{status.get('object_id')}"
            c.log(f"\n  Open: {url}\n  Start with START_HERE.md. Tables are in the catalog '{c.state['catalog']}'.")
            return {"workspace_path": path, "url": url}

        steps += [Step("share", "Share the tables and the sandbox with learners", share, check=lambda c: bool(c.state.get("shared")) and not base["rebuild"]),
                  Step("notebooks", "Upload the SQL notebooks, guides and exercises", notebooks)]
        return steps

    # ---- destroy -----------------------------------------------------------------------------------------------------------------
    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        d = lambda: ctx.services["dbx"]  # noqa: E731

        def preflight(c: PackContext):
            user = login_shared(c).get("userName")
            out: Dict[str, Any] = {"user": user}
            if c.state.get("schemas") or c.state.get("catalog_created"):
                found = self._warehouse(c)
                out["warehouse_id"] = found["warehouse_id"]
                if "sql" not in c.services:
                    c.mem["sql"] = Sql(c.services["dbx"], found["warehouse_id"])
            return out

        def schemas(c: PackContext):
            sql, cat = self._sql(c), c.state["catalog"]
            for name in reversed(list(c.state.get("schemas") or [])):
                c.log(f"  dropping {cat}.{name}")
                sql.run(f"DROP SCHEMA IF EXISTS {cat}.{name} CASCADE")
                self._remember(c, schemas=[s for s in c.state["schemas"] if s != name])
            return {}

        def drop_catalog(c: PackContext):
            self._sql(c).run(f"DROP CATALOG IF EXISTS {c.state['catalog']} CASCADE")
            return {"catalog_created": False}

        def folder(c: PackContext):
            if c.state.get("workspace_path"):
                d().text("workspace", "delete", c.state["workspace_path"], "--recursive")
            return {"workspace_path": None, "url": None}

        def warehouse(c: PackContext):
            d().text("warehouses", "delete", c.state["warehouse_id"])
            return {"warehouse_created": False}

        return [Step("preflight", "Check login", preflight),
                Step("schemas", "Drop every schema this pack created (tables, volumes and sandbox work in them are deleted)", schemas,
                     check=lambda c: not c.state.get("schemas")),
                Step("catalog", "Drop the catalog, because this pack created it", drop_catalog, check=lambda c: not c.state.get("catalog_created")),
                Step("folder", "Delete the uploaded notebooks folder", folder, check=lambda c: not c.state.get("workspace_path")),
                Step("warehouse", "Delete the warehouse, because this pack created it", warehouse, check=lambda c: not c.state.get("warehouse_created"))]

    def outputs(self, state: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in state.items() if k in self.output_keys}
        doms = state.get("domains") or {}
        out["domains"] = {k: {"tables": len(v.get("rows") or {}), "optional_skipped": len(v.get("skipped") or []) + len(v.get("governance_skipped") or [])}
                          for k, v in doms.items()}
        if state.get("grant_failures"):
            out["grant_failures"] = state["grant_failures"]
        return out
