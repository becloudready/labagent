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

## rag-lakebase

RAG app on Databricks Lakebase (Postgres + pgvector) with a pre-loaded 25-passage knowledge base, deployed as a Databricks App.

```bash
pip install 'labagent[rag]'      # needs the Databricks CLI, then: databricks auth login
labagent launch rag-lakebase --name demo
```

Steps: preflight (login, embedding and chat endpoints, Free Edition one-project limit), project, endpoint, pgvector schema, load, HNSW index, app.
Free Edition limits (per the [limitations page](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations)): one Lakebase project, 3 apps, and apps stop after 24 hours.
