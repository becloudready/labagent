"""aml: a synthetic anti-money-laundering lab with ground truth, for two audiences.

mode=rag      Developers build a RAG app in Python. The pack generates the data, builds the corpus, indexes it, runs the quality checks
              and leaves a lab folder with a guide and a small Python helper.
mode=copilot  Non-technical staff learn Microsoft 365 Copilot on ordinary Excel, Word and PDF files. (Added in a later change.)

Everything the pack creates lives in one folder (`out`, default ./aml-<name>) marked with .labagent-aml. `destroy` removes only what
the pack wrote there. The pack touches no cloud account in the local platform.
"""
from __future__ import annotations

import json
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
OWN_PATHS = ("tables", "answer_key", "data", "corpus", "index", tools.CONFIG, GUIDE, SHIM, MARKER)

SHIM_TEXT = '"""Lab helper. Run Python in this folder:  from aml_tools import AmlLab; lab = AmlLab()"""\nfrom labagent.packs.aml.rag.tools import AmlLab  # noqa: F401\n'


def guide_text(cfg: Dict[str, Any], quality: Dict[str, Any]) -> str:
    j = {"ca": "Canada", "us": "the United States", "both": "Canada and the United States"}[cfg["jurisdiction"]]
    q = quality
    note = ("\n**Your embedding model is the keyword-only fallback.** Search matches shared words, not meaning, so some answers will miss. "
            "Install the full lab with `pip install 'labagent[rag-local]'` and launch again.\n") if q["keyword_only_embedder"] else ""
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
| `aml_tools.py` | Helpers for the tasks below |

## Starting numbers

- Retrieval hit rate on {q['retrieval']['questions']} questions with known answers: **{q['retrieval']['hit_rate']:.2f}**
- Citation validity of the reference narratives: **{q['citations']['rate']:.2f}**
- A fixed reference rule agrees with the answer key on **{q['triage']['reference_rule']['agreement']:.2f}** of {q['triage']['alerts']} sample alerts. This shows the evidence is enough to decide. It is not a measure of a model.

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
    output_keys = ("out_dir", "mode", "platform", "jurisdiction", "counts", "quality", "guide", "embedding_model")
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
        Param("llm", "none", "none (mock answers) | env (use the chat model configured in the environment, and score it on triage)"),
        Param("dataset", "aml-synthetic", "generator=snapshot: the pinned dataset name"),
        Param("min_hit_rate", Q.HIT_GATE, "Retrieval hit rate the launch must reach (not applied to the keyword-only fallback)"),
    ]

    # ---- helpers ---------------------------------------------------------------------------------------------------------------
    def services(self, profile: Optional[str]) -> Dict[str, Any]:
        return {}

    def out_dir(self, ctx: PackContext) -> Path:
        return Path(ctx.params["out"] or f"aml-{ctx.name}").expanduser().resolve()

    def _check_params(self, ctx: PackContext) -> None:
        p = ctx.params
        if p["mode"] not in ("rag", "copilot"):
            raise PackError("mode must be rag or copilot")
        if p["mode"] == "copilot":
            raise PackError("mode=copilot is not available in this build yet. Use mode=rag.")
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
               "sample_alert": ctx.state.get("sample_alert", "AL00001")}
        (out / tools.CONFIG).write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        return cfg

    # ---- steps -----------------------------------------------------------------------------------------------------------------
    def deploy_steps(self, ctx: PackContext) -> List[Step]:
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

        return [
            Step("preflight", "Check the options and the output folder", preflight, check=lambda c: (out / MARKER).exists() and "out_dir" in c.state),
            Step("data", "Generate the synthetic transactions, customers, alerts and answer key", data,
                 check=lambda c: "counts" in c.state and (out / "tables" / "transactions.csv").exists()),
            Step("corpus", "Write the policies, case files and regulation text", corpus,
                 check=lambda c: "chunks" in c.state and (out / "corpus" / "chunks.jsonl").exists()),
            Step("index", "Index the corpus for search", index, check=lambda c: "indexed_chunks" in c.state),
            Step("quality", "Run the quality checks", quality, check=lambda c: bool(c.state.get("quality", {}).get("passed"))),
            Step("guide", "Write the lab guide", guide, check=lambda c: (out / GUIDE).exists() and "guide" in c.state),
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
