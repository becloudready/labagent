# Lab packs

A pack is one lab: ordered, idempotent steps behind `launch` and `destroy`. Packs run on the learner's machine with their own
credentials, or in your CI. Add a lab by writing a `Pack` subclass; the framework, CLI and result envelope do not change.

```bash
labagent packs                                   # list
labagent launch rag-sec-tutorial --name demo --dry-run
labagent launch rag-sec-tutorial --name demo         # re-run to resume after a failure
labagent destroy rag-sec-tutorial --name demo        # removes only what this pack created
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

## RAG tutorial (Databricks Lakebase)

`rag-sec-tutorial` builds nothing itself. It uploads nine notebooks and the app code into your workspace, and you run them in order.
Each notebook is short, safe to re-run, and ends with a check cell that stops with a clear message if the step did not work.

| Notebook | You |
|---|---|
| `00_setup` | Check your model endpoints, create the Lakebase project |
| `01_data` | Download the pinned SEC 10-K dataset (checked against a SHA-256) and explore it |
| `02_embeddings` | Call the embedding model, compare similarity, time batches |
| `03_pgvector` | Create the table, load the filings in two stages (everyone but Apple, then Apple) to see why embedding decides what search can find |
| `04_search` | Add HNSW and full-text indexes, run vector, keyword and hybrid (reciprocal rank fusion) search, add filters |
| `05_answer_and_eval` | Generate cited answers, try a question with no answer, score retrieval against a 0.6 gate |
| `06_deploy_app` | Create the Databricks App, give its own identity a read-only role, deploy |
| `99_cleanup` | Remove the app, the schema and the database project |

```bash
pip install 'labagent[rag]'      # needs the Databricks CLI, then: databricks auth login
labagent launch rag-sec-tutorial --name demo
```

It prints a link to the uploaded folder. Open it, start with `00_setup`, and finish with `99_cleanup`. The notebooks run on serverless
compute, need no cluster, and download the dataset themselves. Free Edition allows one Lakebase project and three apps, and stops apps
after 24 hours. `labagent destroy rag-sec-tutorial --name demo` removes only the uploaded folder: run `99_cleanup` first to remove what the
notebooks created.

The one-command versions of this lab (`rag-lakebase`, `rag-sec-filings`, `rag-sec-filings-live`) were removed in favour of the tutorial.
Anyone who types one of those names gets a message pointing here. They are still in the `v0.2.1` release and git tag.

Add a data source by subclassing `labagent.packs.rag.sources.Source` and registering it under the `labagent.rag_sources` entry point.

## Publishing a dataset snapshot (maintainers)

```bash
labagent dataset --chunks chunks.jsonl --name sec-10k --version 2026.11 --out out/
gh release create sec-10k-2026.11 out/* --repo becloudready/labagent-datasets
```

`chunks.jsonl` has one JSON object per line: `id`, `title`, `content`, `meta`. The build is byte-for-byte reproducible. Then add the release
URL and the printed `sha256` to `labagent/packs/rag/sources/datasets.json` in a pull request. Data limitations (for example, no Item 7 for
JPM and CVX) are listed in the dataset README.

The SEC chunks were produced by the `rag-sec-filings-live` pack, which no longer exists. The parser it used is still in the code
(`labagent.packs.rag.sources.sec.SecFilings`) but nothing calls it any more. To rebuild the SEC dataset from EDGAR, use the `v0.2.1` tag.

## db-agent (text-to-SQL on Databricks)

Deploys [db-agent](https://github.com/db-agent/db-agent), the open-source text-to-SQL agent, to Databricks Apps in your own workspace.
labagent contains none of db-agent's code: it clones the repo at a pinned commit, writes an `app.yaml` for your workspace, and deploys it.

```bash
labagent launch db-agent --name demo                              # app: db-agent-demo, data: samples.bakehouse
labagent launch db-agent --name demo --param dataset=tpch         # any schema in the samples catalog
labagent destroy db-agent --name demo
```

Steps: `preflight` (login, SQL warehouse, sample data, serving endpoints), `fetch` (pinned commit), `token`, `app`, `verify`
(asks a real question through the deployed app: this proves the token, warehouse, LLM and SQL safety layer work together).

- **Pinned code.** `ref` must be a full commit SHA, and the clone is refused if HEAD differs. To move to a newer db-agent, review it and change the pin.
- **Data.** The `samples` catalog already exists in every workspace, so nothing is downloaded or redistributed.
- **Credential.** The app needs a token for the warehouse and the LLM. labagent creates one with a 30-day lifetime (`--param token_days`), stores it
  only as an app secret (via stdin, never on a command line, in state or in output), and `destroy` revokes it. If your workspace blocks
  token creation, create one yourself and `export LABAGENT_DATABRICKS_TOKEN=...`; labagent then uses it and never revokes it.
- **Free Edition.** Up to 3 apps per account, and apps stop after 24 hours.

## Plain English

```bash
labagent "Run and deploy rag-tutorial in databricks"      # shows the command, asks, then runs it
labagent "tear down rag-tutorial" --print-only            # just show the command
labagent ask what can you deploy                     # 'ask' lets you skip the quotes
```

A small model reads the sentence and picks an action (launch, destroy, list) and a pack. By default that is `qwen2.5-coder:0.5b`
(about 400 MB) on local Ollama, so it runs on any laptop: `ollama pull qwen2.5-coder:0.5b`.
Any OpenAI-compatible endpoint works: `--endpoint`, `--model`, or `LABAGENT_LLM_URL`, `LABAGENT_LLM_MODEL`, `LABAGENT_LLM_API_KEY`.
A remote endpoint is announced before your sentence is sent to it.

The model never runs anything, and it is not trusted for details:
- The instance name (`called x`) and parameters (`profile DEFAULT`, `tickers AAPL,JPM`) are read from your sentence by exact rules, so they cannot be invented.
- A pack you typed (`rag-tutorial`, or any id or alias) wins over the model's pick. If the model guessed, the pack needs a distinctive word from your sentence.
- Requests that ask to create and delete in one sentence are refused. A delete needs a delete word, and you confirm by typing the instance name.
- Without a terminal, nothing runs unless you pass `--yes`.

Measured on `qwen2.5-coder:0.5b` with 42 phrasings (25 used while tuning, 17 unseen): all handled correctly, including refusals.
That is a small, author-written test set, so expect misses on unusual wording. A refusal is the usual failure, and the command shown is always checked by you.
