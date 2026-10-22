# Lab packs

A pack is one lab: ordered, idempotent steps behind `launch` and `destroy`. Packs run on the learner's machine with their own
credentials, or in your CI. Add a lab by writing a `Pack` subclass; the framework, CLI and result envelope do not change.

```bash
labagent packs                                   # list
labagent launch rag-lakebase --name demo --dry-run
labagent launch rag-lakebase --name demo         # re-run to resume after a failure
labagent destroy rag-lakebase --name demo        # removes only what this pack created
```

## Writing a pack

```python
from labagent.packs.base import Pack, Param, Step

class MyLab(Pack):
    id = "my-lab"; summary = "what it builds"
    params = [Param("size", "small")]
    def deploy_steps(self, ctx):
        return [Step("make", "Create the thing", lambda c: {"thing_id": "..."}, check=lambda c: "thing_id" in c.state)]
    def destroy_steps(self, ctx): ...
```

Ship it in your own package and register it: `[project.entry-points."labagent.packs"] my-lab = "mypkg:MyLab"`.
Rules: `check` is a read-only "already done" test; step outputs are non-secret; tokens stay in memory (`ctx.mem`).
State is a 0600 file at `~/.labagent/packs/<pack>/<name>.json`.

## RAG packs (Databricks Lakebase)

One reusable pipeline, pluggable data sources. Each stage is idempotent, and `evaluate` gates the deploy:

`preflight, project, locate, build (fetch, parse, chunk), schema, load (embed), index, evaluate, app`

| Pack | Source | Use |
|---|---|---|
| `rag-sec-filings` | Real SEC 10-K filings from EDGAR, split into Items (Business, Risk Factors, MD&A) | Financial research |
| `rag-lakebase` | 25 invented passages | Smoke test only |

```bash
pip install 'labagent[rag]'      # needs the Databricks CLI, then: databricks auth login
labagent launch rag-sec-filings --name sec --param "user_agent=Full Name name@example.com" \
    --param tickers=AAPL,JPM,PFE --param filings=2
```

What makes it more than a toy: section-aware chunking, hybrid retrieval (pgvector + full-text, rank-fused), metadata filters
(`{"ticker": "AAPL"}`), numbered citations with the filing URL, and an evaluation set that must reach `min_hit_rate`
(default 0.6) before the app is deployed. Filings are downloaded at launch (EDGAR rules: declared contact, under 10 requests per second,
cached locally) and are never stored in this repo.

Free Edition allows one Lakebase project: pass `--param project=<existing>` to share it. Each source uses its own schema, and
`destroy` drops only that schema (and the project only if labagent created it). Apps stop after 24 hours.

Add a source by subclassing `labagent.packs.rag.sources.Source` (`chunks()` and `eval_set()`) and registering it under the
`labagent.rag_sources` entry point.
