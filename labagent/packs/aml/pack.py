"""aml: a synthetic anti-money-laundering lab with ground truth, for two audiences.

mode=rag      Developers build a RAG app in Python. The pack generates the data, builds the corpus, indexes it, runs the quality checks
              and leaves a lab folder with a guide and a small Python helper.
mode=copilot  Non-technical staff learn Microsoft 365 Copilot on ordinary Excel, Word and PDF files. (Added in a later change.)

Everything the pack creates lives in one folder (`out`, default ./aml-<name>) marked with .labagent-aml. `destroy` removes only what
the pack wrote there. The pack touches no cloud account in the local platform.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ... import datasets
from ..base import Pack, PackContext, PackError, Param, Step
from . import gen as generators
from .corpus import build as corpus_build
from .corpus import documents as D
from .corpus import regs as R
from .schema import TABLES, table_counts, validate, write_csv
from .rag import quality as Q
from .rag import tools

MARKER = ".labagent-aml"
GUIDE = "LAB_GUIDE.md"
SHIM = "aml_tools.py"
OWN_PATHS = ("tables", "answer_key", "data", "corpus", "index", "solutions", tools.CONFIG, GUIDE, SHIM, MARKER)

SHIM_TEXT = '"""Lab helper. Run Python in this folder:  from aml_tools import AmlLab; lab = AmlLab()"""\nfrom labagent.packs.aml.rag.tools import AmlLab  # noqa: F401\n'


def guide_text(cfg: Dict[str, Any], quality: Dict[str, Any]) -> str:
    j = {"ca": "Canada", "us": "the United States", "both": "Canada and the United States"}[cfg["jurisdiction"]]
    q = quality
    note = ("\n**Your embedding model is the keyword-only fallback.** Search matches shared words, not meaning, so some answers will miss. "
            "Install the full lab with `pip install 'labagent[rag-local]'` and launch again.\n") if q["keyword_only_embedder"] else ""
    solutions_line = "\n   The instructor copies with the answers are in `solutions/`." if cfg.get("solutions") else ""
    return f"""# AML RAG lab: {cfg['institution']} ({j})

You build a research assistant for an anti-money laundering team. It answers policy questions with citations, pulls the evidence for an
alert, and helps draft a report narrative. All names and transactions are invented. {cfg['institution']} is not a real institution.
{note}
## What is in this folder

| Path | What it holds |
|---|---|
| `data/` | Policy manual, alert procedure, report guide and one case file per alert (Markdown). Start `labagent rag serve --dir .` to search them. |
| `tables/` | `customers`, `accounts`, `transactions`, `alerts`, `kyc_profiles`, `sanctions_hits` as CSV |
| `answer_key/` | The decisions and reasons for each alert. Open it after you decide. |
| `aml_tools.py` | The finished assistant (the demo) |
| `notebooks/` | Four build-it-yourself notebooks (the lab) |

## Starting numbers

- Retrieval hit rate on {q['retrieval']['questions']} questions with known answers: **{q['retrieval']['hit_rate']:.2f}**
- Citation validity of the reference narratives: **{q['citations']['rate']:.2f}**
- A fixed reference rule agrees with the answer key on **{q['triage']['reference_rule']['agreement']:.2f}** of {q['triage']['alerts']} sample alerts. This shows the evidence is enough to decide. It is not a measure of a model.

## Two ways to use this folder

1. **The demo.** `aml_tools.py` is a finished assistant. The tasks below use it, so you can see what a good result looks like.
2. **The lab.** `notebooks/` holds four notebooks where you build the same parts yourself and test each against the data and the answer key:
   `01_policy_search`, `02_evidence_bundle`, `03_citations_and_narrative`, `04_triage_and_evaluation`. Open them with `jupyter lab`
   (`pip install jupyterlab`). Do the demo tasks first, then the notebooks.{solutions_line}

## Task 1: ask policy questions with citations

```python
from aml_tools import AmlLab
lab = AmlLab()
r = lab.ask("When must a suspicious transaction report be filed?")
print(r.answer)
for c in r.chunks: print(c.rank, c.metadata.get("citation") or c.chunk_id)
```

Try a filter: `lab.ask("How long are records kept?", doc_type="policy")`. Find one question the search gets wrong and say why.

## Task 2: pull the evidence for an alert

```python
lab.show("{cfg['sample_alert']}")
```

The bundle has the customer profile, the alert trigger, the transactions around it, the matching policy and rule text, and similar earlier cases.
Read it and decide: **escalate**, **monitor** or **dismiss**. Write down the transaction IDs that support your decision.

## Task 3: draft a narrative and check it

Write a short narrative (who, what, when, where, why, how) for an alert you would escalate. Cite transaction IDs and the policy section.

```python
print(lab.check(my_narrative))
```

Every cited ID and section must exist. Fix any that do not. Then compare: `lab.score({{"{cfg['sample_alert']}": "escalate"}})`.

## Task 4 (optional): let a chat model decide

Set a provider key (for example `GROQ_API_KEY`) and run `lab.triage("{cfg['sample_alert']}")` on ten alerts. Score the answers with `lab.score`. Where does it disagree with you, and why?

## Cleaning up

`labagent destroy aml --name {cfg['name']}` removes this folder's lab files.
"""


class AmlLab(Pack):
    id = "aml"
    title = "Synthetic AML lab (RAG and Copilot)"
    summary = "Synthetic anti-money laundering data with ground truth, policies and regulations: a RAG lab for developers, a Copilot lab for staff"
    aliases = ("aml-rag", "aml-lab", "anti-money-laundering", "aml-copilot")
    platforms = ("local",)
    cost_tier = "free"
    default_name = "demo"
    output_keys = ("out_dir", "mode", "platform", "jurisdiction", "counts", "quality", "guide", "embedding_model", "learner_dir", "instructor_dir",
                   "learner_zip", "instructor_zip", "package", "notebooks")
    params = [
        Param("mode", "rag", "rag (developers: indexed corpus and Python helpers) | copilot (staff: Excel, Word and PDF files)"),
        Param("jurisdiction", "ca", "ca | us | both"),
        Param("size", "small", "small (about 300 customers, 40k transfers) | medium (about 2,000 customers, 280k transfers)"),
        Param("generator", "builtin", "builtin (pure Python, deterministic) | snapshot (pinned release from labagent-datasets)"),
        Param("seed", 42, "Same seed, same data"),
        Param("platform", "local", "local"),
        Param("out", None, "Folder for the lab files (default ./aml-<name>)"),
        Param("regulations", "auto", "auto | fetch (download the regulation text now) | skip (policies and cases only)"),
        Param("embedder", "auto", "auto (full model if installed, else keyword-only fallback) | hashing (force the fast keyword-only embedder)"),
        Param("solutions", "no", "rag: yes also writes the solved notebooks to solutions/ (for instructors)"),
        Param("llm", "none", "none (mock answers) | env (use the chat model configured in the environment, and score it on triage)"),
        Param("dataset", "aml-synthetic", "generator=snapshot: the pinned dataset name"),
        Param("cases", 20, "copilot: how many case workbooks to include, 12 to 60"),
        Param("guidance", "fetch", "copilot, Canada: fetch (download the FINTRAC guidance pages now) | skip"),
        Param("cohort", None, "copilot: name of the folder and zip (default: the instance name)"),
        Param("min_hit_rate", Q.HIT_GATE, "Retrieval hit rate the launch must reach (not applied to the keyword-only fallback)"),
    ]

    # ---- helpers ---------------------------------------------------------------------------------------------------------------
    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {}

    def intent_params(self, text: str) -> Dict[str, str]:
        """"set up the AML copilot lab for Canada" -> mode=copilot, jurisdiction=ca."""
        low = text.lower()
        out: Dict[str, str] = {}
        if re.search(r"\b(copilot|co-pilot|microsoft 365|excel|staff|non-technical)\b", low):
            out["mode"] = "copilot"
        elif re.search(r"(?<![\w-])(rag|developer|developers|python)(?![\w-])", low):
            out["mode"] = "rag"
        ca = re.search(r"\b(canada|canadian|fintrac|pcmltfa)\b", low)
        us = re.search(r"\b(united states|usa|america|american|fincen|bank secrecy act|the us|the u\.s\.)(?![\w-])", low)
        if (ca and us) or re.search(r"\bboth (countries|jurisdictions)\b", low):
            out["jurisdiction"] = "both"
        elif ca:
            out["jurisdiction"] = "ca"
        elif us:
            out["jurisdiction"] = "us"
        if re.search(r"\b(medium|large)\b", low):
            out["size"] = "medium"
        return out

    def out_dir(self, ctx: PackContext) -> Path:
        return Path(ctx.params["out"] or f"aml-{ctx.name}").expanduser().resolve()

    def _check_params(self, ctx: PackContext) -> None:
        p = ctx.params
        if p["mode"] not in ("rag", "copilot"):
            raise PackError("mode must be rag or copilot")
        if p["guidance"] not in ("fetch", "skip"):
            raise PackError("guidance must be fetch or skip")
        try:
            if int(p["cases"]) < 12 or int(p["cases"]) > 60:
                raise ValueError
        except (TypeError, ValueError):
            raise PackError("cases must be a number from 12 to 60") from None
        if p["jurisdiction"] not in ("ca", "us", "both"):
            raise PackError("jurisdiction must be ca, us or both")
        if p["size"] not in ("small", "medium") and not ctx.services.get("allow_tiny"):
            raise PackError("size must be small or medium")
        if p["generator"] not in ("builtin", "snapshot"):
            raise PackError("generator must be builtin or snapshot")
        if p["platform"] != "local":
            raise PackError("platform must be local")
        if p["regulations"] not in ("auto", "fetch", "skip"):
            raise PackError("regulations must be auto, fetch or skip")
        if p["solutions"] not in ("no", "yes"):
            raise PackError("solutions must be no or yes")
        if p["embedder"] not in ("auto", "hashing") or p["llm"] not in ("none", "env"):
            raise PackError("embedder must be auto or hashing, and llm must be none or env")
        try:
            int(p["seed"])
        except (TypeError, ValueError):
            raise PackError("seed must be a whole number") from None

    def _client(self, ctx: PackContext, out: Path):
        make = ctx.services.get("client")
        if make:
            return make(out)
        return tools.open_client(out)

    def _write_config(self, ctx: PackContext, out: Path) -> Dict[str, Any]:
        p = ctx.params
        cfg = {"institution": "Maple Ridge Credit Union", "name": ctx.name, "jurisdiction": p["jurisdiction"], "mode": p["mode"],
               "regulations": p["regulations"] != "skip", "embedder": p["embedder"], "seed": int(p["seed"]), "size": p["size"],
               "sample_alert": ctx.state.get("sample_alert", "AL00001"), "solutions": p["solutions"] == "yes"}
        (out / tools.CONFIG).write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        return cfg

    # ---- steps -----------------------------------------------------------------------------------------------------------------
    def copilot_steps(self, ctx: PackContext) -> List[Step]:
        from .copilot import guidance as G
        from .copilot import package as P
        from .schema import read_csv
        out = self.out_dir(ctx)

        def preflight(c: PackContext):
            self._check_params(c)
            if out.exists() and not (out / MARKER).exists() and any(out.iterdir()):
                raise PackError(f"{out} already exists and was not created by this pack. Choose another folder with --param out=...")
            out.mkdir(parents=True, exist_ok=True)
            (out / MARKER).write_text("created by labagent pack aml\n")
            return {"out_dir": str(out), "mode": "copilot", "platform": "local", "jurisdiction": c.params["jurisdiction"], "created": [MARKER]}

        def data(c: PackContext):
            try:
                ds = generators.get(c.params["generator"]).generate(
                    seed=c.params["seed"], size=c.params["size"], jurisdiction=c.params["jurisdiction"], dataset=c.params["dataset"])
            except (datasets.DatasetError, KeyError) as e:
                raise PackError(str(e)) from e
            problems = validate(ds.tables)
            if problems:
                raise PackError("generated data failed validation: " + "; ".join(problems[:3]))
            work = out / ".work" / "tables"
            names = write_csv(ds.tables, work)
            return {"counts": table_counts(ds.tables), "data_sources": ds.sources,
                    "created": c.state.get("created", []) + [str(p.relative_to(out)) for p in names]}

        def package(c: PackContext):
            tables = read_csv(out / ".work" / "tables")
            http = c.services.get("regs_http") or R.default_http
            chunks: List[Dict[str, Any]] = []
            if c.params["regulations"] != "skip":
                try:
                    chunks = R.regulation_chunks(c.params["jurisdiction"], http=http, cache_dir=c.services.get("regs_cache"))
                except (OSError, ValueError) as e:
                    raise PackError(f"could not download the regulation text ({type(e).__name__}). Check your network, or launch with --param regulations=skip") from e
            notes: List[str] = []
            texts: Dict[str, str] = {}
            if c.params["guidance"] == "fetch" and c.params["jurisdiction"] in ("ca", "both"):
                texts, notes = G.fetch_guidance(c.services.get("guidance_http") or R.default_http)
            cohort = c.params["cohort"] or c.name
            try:
                res = P.build_package(tables, c.params["jurisdiction"], cohort, out, int(c.params["cases"]), chunks, texts)
            except OSError as e:
                raise PackError(f"could not write the files ({type(e).__name__}). Check the folder and the free space.") from e
            reasons = [g["reason"] for g in tables["ground_truth"]]
            problems = P.verify_package(Path(res["learner_dir"]), Path(res["instructor_dir"]), reasons)
            if problems:
                raise PackError("the folder failed its own checks: " + "; ".join(problems[:3]))
            summary = {"files": res["files"], "cases": res["cases"], "exercises": res["exercises"], "regulation_excerpts": res["excerpts"],
                       "fintrac_guidance": res["guidance"], "guidance_notes": notes, "checks_passed": True}
            return {"learner_dir": res["learner_dir"], "instructor_dir": res["instructor_dir"], "learner_zip": res["learner_zip"],
                    "instructor_zip": res["instructor_zip"], "package": summary, "created": c.state.get("created", []) + res["created"]}

        return [
            Step("preflight", "Check the options and the output folder", preflight, check=lambda c: (out / MARKER).exists() and "out_dir" in c.state),
            Step("data", "Generate the synthetic customers, transfers and alerts", data, check=lambda c: "counts" in c.state and (out / ".work" / "tables" / "transactions.csv").exists()),
            Step("package", "Build the Excel, Word and text files, the exercise guide and the zips", package,
                 check=lambda c: bool(c.state.get("package", {}).get("checks_passed")) and Path(c.state.get("learner_zip", "")).exists()),
        ]

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        if ctx.params.get("mode") == "copilot":
            return self.copilot_steps(ctx)
        out = self.out_dir(ctx)

        def preflight(c: PackContext):
            self._check_params(c)
            if out.exists() and not (out / MARKER).exists() and any(out.iterdir()):
                raise PackError(f"{out} already exists and was not created by this pack. Choose another folder with --param out=...")
            out.mkdir(parents=True, exist_ok=True)
            (out / MARKER).write_text("created by labagent pack aml\n")
            return {"out_dir": str(out), "mode": c.params["mode"], "platform": "local", "jurisdiction": c.params["jurisdiction"]}

        def data(c: PackContext):
            try:
                ds = generators.get(c.params["generator"]).generate(
                    seed=c.params["seed"], size=c.params["size"], jurisdiction=c.params["jurisdiction"], dataset=c.params["dataset"])
            except (datasets.DatasetError, KeyError) as e:
                raise PackError(str(e)) from e
            problems = validate(ds.tables)
            if problems:
                raise PackError("generated data failed validation: " + "; ".join(problems[:3]))
            write_csv(ds.tables, out / "tables", only=[n for n in TABLES if n != "ground_truth"])
            write_csv(ds.tables, out / "answer_key", only=["ground_truth"])
            (out / "tables" / "provenance.json").write_text(json.dumps({"sources": ds.sources, "info": ds.info}, indent=2, sort_keys=True) + "\n")
            first = next((g["alert_id"] for g in ds.tables["ground_truth"] if g["label"] == "escalate"), "AL00001")
            return {"counts": table_counts(ds.tables), "sample_alert": first, "data_sources": ds.sources}

        def corpus(c: PackContext):
            from .schema import read_csv
            tables = read_csv(out / "tables", only=[n for n in TABLES if n != "ground_truth"])
            tables.update(read_csv(out / "answer_key", only=["ground_truth"]))
            want_regs = c.params["regulations"] != "skip"
            http = c.services.get("regs_http") or R.default_http
            try:
                chunks = corpus_build.build_chunks(tables, c.params["jurisdiction"], with_regulations=want_regs, http=http, cache_dir=c.services.get("regs_cache"))
            except (OSError, ValueError) as e:
                raise PackError(f"could not download the regulation text ({type(e).__name__}). Check your network, or launch with --param regulations=skip") from e
            (out / "corpus").mkdir(exist_ok=True)
            (out / "corpus" / "chunks.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in chunks) + "\n")
            docs = D.policy_documents(c.params["jurisdiction"]) + D.case_documents(tables, c.params["jurisdiction"])
            corpus_build.write_markdown(docs, out / "data")
            if want_regs:
                reg_by = {}
                for x in chunks:
                    if x["meta"]["doc_type"] == "regulation":
                        reg_by.setdefault(x["meta"]["instrument"], []).append(x)
                for inst, rows in reg_by.items():
                    slug = inst.lower().replace(" ", "-")
                    body = [f"# {inst}", "", rows[0]["meta"]["notice"], ""]
                    for x in rows:
                        body += [f"## {x['title']}", "", x["content"], ""]
                    (out / "data" / f"regulation-{slug}.md").write_text("\n".join(body))
            return {"chunks": len(chunks), "documents": len(docs)}

        def index(c: PackContext):
            self._write_config(c, out)
            client = self._client(c, out)
            rows = [json.loads(l) for l in (out / "corpus" / "chunks.jsonl").read_text().splitlines() if l.strip()]
            res = client.ingest_chunks(rows)
            return {"indexed_chunks": res.chunks, "embedding_model": getattr(client.embedder, "name", "unknown")}

        def quality(c: PackContext):
            from .schema import read_csv
            tables = read_csv(out / "tables", only=[n for n in TABLES if n != "ground_truth"])
            tables.update(read_csv(out / "answer_key", only=["ground_truth"]))
            client = self._client(c, out)
            q = Q.run_quality(client, tables, c.params["jurisdiction"], c.params["regulations"] != "skip",
                              min_hit_rate=float(c.params["min_hit_rate"]), use_chat_model=c.params["llm"] == "env",
                              policy_sections={s.id for d in D.policy_documents(c.params["jurisdiction"]) for s in d.sections})
            c.state["quality_full"] = q
            if not q["passed"]:
                raise PackError("quality check failed: " + "; ".join(q["failures"]))
            return {"quality_full": q, "quality": summarize(q)}

        def guide(c: PackContext):
            cfg = self._write_config(c, out)
            (out / GUIDE).write_text(guide_text(cfg, c.state["quality_full"]))
            (out / SHIM).write_text(SHIM_TEXT)
            return {"guide": str(out / GUIDE)}

        def notebooks(c: PackContext):
            from . import lab
            written = []
            files = dict(lab.student())
            if c.params["solutions"] == "yes":
                files.update(lab.solution())
            for rel, content in files.items():
                path = out / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                written.append(rel)
            return {"notebooks": sorted(written)}

        return [
            Step("preflight", "Check the options and the output folder", preflight, check=lambda c: (out / MARKER).exists() and "out_dir" in c.state),
            Step("data", "Generate the synthetic transactions, customers, alerts and answer key", data,
                 check=lambda c: "counts" in c.state and (out / "tables" / "transactions.csv").exists()),
            Step("corpus", "Write the policies, case files and regulation text", corpus,
                 check=lambda c: "chunks" in c.state and (out / "corpus" / "chunks.jsonl").exists()),
            Step("index", "Index the corpus for search", index, check=lambda c: "indexed_chunks" in c.state),
            Step("quality", "Run the quality checks", quality, check=lambda c: bool(c.state.get("quality", {}).get("passed"))),
            Step("guide", "Write the lab guide", guide, check=lambda c: (out / GUIDE).exists() and "guide" in c.state),
            Step("notebooks", "Write the build-it-yourself notebooks", notebooks,
                 check=lambda c: bool(c.state.get("notebooks")) and all((out / rel).exists() for rel in c.state["notebooks"])),
        ]

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        out = self.out_dir(ctx)

        def remove(c: PackContext):
            if not out.exists():
                return {}
            if not (out / MARKER).exists():
                raise PackError(f"{out} was not created by this pack, so it is left alone")
            for name in OWN_PATHS:
                path = out / name
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            from . import lab
            for rel, content in lab.student().items():     # a notebook the learner has edited is their work: keep it
                path = out / rel
                if path.is_file() and path.read_text() == content:
                    path.unlink()
            for rel in c.state.get("created", []):        # copilot mode lists every file it wrote: delete those, keep anything else
                path = out / rel
                if path.is_file():
                    path.unlink()
            for d in sorted((x for x in out.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
                try:
                    d.rmdir()               # empty folders only
                except OSError:
                    pass
            try:
                out.rmdir()                 # only if empty: anything the learner added stays
            except OSError:
                pass
            return {}

        return [Step("remove", "Delete the lab files this pack wrote", remove)]

    def outputs(self, state: Dict[str, Any]) -> Dict[str, Any]:
        o = {k: v for k, v in state.items() if k in self.output_keys}
        o["embedding_model"] = state.get("embedding_model")
        return o


def summarize(q: Dict[str, Any]) -> Dict[str, Any]:
    chat = q["triage"].get("chat_model")
    return {"passed": q["passed"], "retrieval_hit_rate": q["retrieval"]["hit_rate"], "retrieval_questions": q["retrieval"]["questions"],
            "retrieval_gate": q["retrieval"]["gate"], "citation_validity": q["citations"]["rate"],
            "triage_reference_agreement": q["triage"]["reference_rule"]["agreement"], "triage_alerts": q["triage"]["alerts"],
            "triage_chat_model_agreement": chat["agreement"] if chat else None, "keyword_only_embedder": q["keyword_only_embedder"]}
