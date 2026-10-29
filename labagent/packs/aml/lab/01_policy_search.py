# %% [markdown]
# # AML lab 1: search the policies, with filters
#
# **Time:** about 25 minutes. **Cost:** nothing. **Needs:** the RAG lab notebooks 1 to 6 (or at least 3), and a lab folder made by
# `labagent launch aml --name demo --param mode=rag`.
#
# ## The plan
#
# `labagent launch aml` already gave you a **finished** AML assistant: `aml_tools.AmlLab`, with `ask`, `show`, `check`, `triage` and
# `score`. Try it first, so you know what you are aiming at. Then, in four notebooks, you build the working parts yourself and
# compare with it.
#
# | # | You build | Compared with the finished tool by |
# |---|---|---|
# | 1 | a policy and regulation search with filters | the launch's own 14-question test |
# | 2 | the evidence bundle for an alert | how many key transactions it contains |
# | 3 | a citation checker and the narrative prompt | invented IDs and sections it must catch |
# | 4 | a triage rule and a scorer | the answer key |
#
# All names, accounts and transactions are invented. **Maple Ridge Credit Union is not a real institution.** The laws are real.
#
# ## First, use the finished tool (2 minutes)

# %%
import sys
from pathlib import Path
from labagent.ai.rag.labkit import lab_root

sys.path.insert(0, str(lab_root()))
from aml_tools import AmlLab

demo = AmlLab(lab_root())
r = demo.ask("When must a suspicious transaction report be filed?", top_k=3)
for c in r.chunks:
    print(f"{c.similarity:.2f}  {c.metadata.get('citation') or c.chunk_id}")
demo.show(demo.alerts()[2]["id"])

# %% [markdown]
# That is the target: a search with citations, and an evidence bundle that holds everything a reviewer needs. You will now
# write both. (Every exercise has a check. Nothing in this lab needs a paid key; with no `GROQ_API_KEY` the model answers are a marked mock.)
#
# ## Load the corpus
#
# `corpus/chunks.jsonl` holds the chunks the launch built: policies, procedures, guides, regulation sections and one case file per
# alert. Each chunk carries **metadata** that you will filter on.

# %%
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer
from labagent.ai.rag.labkit import check, lab_root, load_env

load_env()
root = lab_root()
sys.path.insert(0, str(root))
cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
print("jurisdiction:", cfg["jurisdiction"], "| regulations included:", cfg["regulations"])

rows = [json.loads(line) for line in (root / "corpus" / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
print(len(rows), "chunks")
print(Counter((r["meta"]["doc_type"], r["meta"]["jurisdiction"]) for r in rows))
print(json.dumps({k: rows[0][k] for k in ("id", "title")} | {"meta": rows[0]["meta"]}, indent=1)[:600])
#### SOLUTION corpus_setup
#### END

# %% [markdown]
# Three metadata fields matter here. `doc_type` is `policy`, `procedure`, `guide`, `regulation` or `case`. `jurisdiction` is `ca` or
# `us`. `section` (and `citation` for regulations) says where in the document the text sits, which is what an investigator cites.
#
# ## Build the store
#
# You wrote `TinyStore` in the RAG lab. Here is the working version, filled with the AML chunks. Each chunk dict keeps its metadata
# fields at the top level so you can filter on them.

# %%
model = SentenceTransformer("all-MiniLM-L6-v2")

#### REFERENCE TinyStore

chunks = [{"id": r["id"], "text": r["content"], "title": r["title"], **r["meta"]} for r in rows]
store = TinyStore()
store.add(chunks)
print(len(store), "chunks embedded")
#### SOLUTION store_setup
#### END

# %% [markdown]
# ## Exercise 1: search with a filter
#
# `TinyStore.search` ranks **all** chunks. An AML analyst in Canada must never be shown the US rule, and a question about internal
# policy should not return a regulation. Write `search(question, k=5, **where)`:
#
# 1. Keep only the chunks whose fields equal every `where` pair (`doc_type="policy"`, `jurisdiction="ca"`, ...). Apply the filter
#    **before** ranking, so the top `k` all come from the allowed set.
# 2. Score the kept chunks against the question with one matrix-vector product on `store.vectors[...]`.
# 3. Return the `k` best as copies of the chunk dict with a `score`, best first. Return `[]` if nothing matches the filter.

# %%
def search(question, k=5, **where):
    """Top-k chunks for the question, restricted to chunks whose fields equal every pair in `where`."""
    #### SOLUTION search
    keep = [i for i, c in enumerate(store.chunks) if all(c.get(field) == value for field, value in where.items())]
    if not keep:
        return []
    q = model.encode(question, normalize_embeddings=True)
    scores = store.vectors[keep] @ q
    best = np.argsort(-scores)[:k]
    return [{**store.chunks[keep[i]], "score": float(scores[i])} for i in best]
    #### STUDENT
    # TODO: indexes of the chunks that match every pair in `where`
    # TODO: embed the question, score only those rows, take the k best
    return []
    #### END

# %%
hits = search("When must a suspicious transaction report be filed?", k=5)
check("returns k results, best first", len(hits) == 5 and all(a["score"] >= b["score"] for a, b in zip(hits, hits[1:])), "sort by score, highest first")
pol = search("How long must records be kept?", k=5, doc_type="policy")
check("a doc_type filter keeps only that type", pol and all(h["doc_type"] == "policy" for h in pol), "filter before ranking")
j = chunks[0]["jurisdiction"]
two = search("record keeping", 3, doc_type="policy", jurisdiction=j)
check("two filters combine", two and all(h["doc_type"] == "policy" and h["jurisdiction"] == j for h in two), "every pair in `where` must match")
check("an impossible filter returns nothing", search("anything", 3, doc_type="no-such-type") == [], "return [] when no chunk matches")
check("the stored chunks are not changed", "score" not in store.chunks[0], "build new dicts for the results")

# %% [markdown]
# ## Why the filter matters
#
# In a lab built with `jurisdiction=both`, the corpus holds the Canadian and the US rules. The same question has two right answers,
# one for each country. Run it with and without a filter. (With a single-country lab both runs give the same chunks, which is also
# correct. Say why.)

# %%
for j in sorted({c["jurisdiction"] for c in chunks}):
    print(f"jurisdiction={j}")
    for h in search("When must a suspicious transaction report be filed?", k=3, jurisdiction=j):
        print(f"  {h['score']:.2f}  {h.get('citation') or h['id']}")

# %% [markdown]
# ## Exercise 2: measure your search
#
# The launch checks retrieval with a fixed set of questions that each name the metadata a correct chunk must carry. Run the same
# set on **your** search. `hit_rate(cases, k)` returns the share of cases where one of the top `k` chunks carries every field in
# the case's `expect` dict (compare as strings).

# %%
from labagent.packs.aml.rag.qa import policy_qa

cases = policy_qa(cfg["jurisdiction"], cfg["regulations"])
print(len(cases), "questions. Example:", cases[0])

def hit_rate(cases, k=5, use_jurisdiction_filter=False):
    """Share of cases with a chunk in the top k that carries every field in case['expect']. Also return the failed questions."""
    #### SOLUTION hit_rate
    misses = []
    for case in cases:
        where = {"jurisdiction": case["expect"]["jurisdiction"]} if use_jurisdiction_filter else {}
        top = search(case["question"], k, **where)
        if not any(all(str(h.get(f)) == str(v) for f, v in case["expect"].items()) for h in top):
            misses.append(case["question"])
    return (len(cases) - len(misses)) / len(cases), misses
    #### STUDENT
    # TODO: for each case, search (with the jurisdiction filter if asked), and test whether any hit carries every field of case["expect"]
    return 0.0, []
    #### END

# %%
rate, misses = hit_rate(cases)
rate_f, misses_f = hit_rate(cases, use_jurisdiction_filter=True)
print(f"hit rate {rate:.2f} without the filter, {rate_f:.2f} with it, on {len(cases)} questions")
check("the rate is a share between 0 and 1", 0 < rate <= 1 and 0 < rate_f <= 1, "divide hits by the number of cases")
check("it reaches the launch gate of 0.6", rate_f >= 0.6, "the launch gate is 0.6; below it, look at the misses")
check("the filter never lowers the rate", rate_f >= rate, "a filter on the right country can only remove wrong-country hits")
for m in misses_f[:5]:
    top = search(m, 1)[0]
    print("MISS:", m, "\n      top hit:", top.get("citation") or top["id"], "|", top["text"][:90].replace("\n", " "))

# %% [markdown]
# **Your turn (write your answer):** pick one miss. Read the top hit and the chunk that should have won. Is the cause the chunking
# (the answer is split), the wording (the question and the policy use different words), or the data (the answer is in another
# section)? What would you change first?
#
# *Your answer:*
#
# ## Compare with the finished tool
#
# `AmlLab.ask` uses the library's retrieval (800-character chunks plus a keyword rerank). Your numbers will differ.

# %%
print("finished tool:", demo.ask("How long must records be kept?", top_k=1).chunks[0].metadata.get("citation") or "policy section")
print("yours        :", (search("How long must records be kept?", 1)[0].get("citation") or "policy section " + str(search("How long must records be kept?", 1)[0]["section"])))

# %% [markdown]
# ## What to take away
#
# - A **filter** is not an optimisation. In compliance work it is part of correctness: the wrong country's rule is a wrong answer.
# - **Metadata is built at ingestion.** Every `section` and `citation` here was recorded when the chunks were made. A citation you
#   cannot trace to a chunk is a citation you cannot defend.
# - Measure retrieval with questions that have a known right place, as the launch does. A hit rate of 0.8 means one question in
#   five finds the wrong passage.
#
# **Next:** notebook 2 gathers the evidence for one alert: who the customer is, what moved, which policy applies, and what happened
# on similar cases.
