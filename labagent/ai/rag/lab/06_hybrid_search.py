# %% [markdown]
# # 6. Hybrid search: when exact words matter
#
# **Time:** about 25 minutes. **Cost:** nothing. **Needs:** notebooks 1 to 5.
#
# Embeddings match meaning. That is a strength until someone searches for a form number, an error code or a product name. A
# meaning model has no special respect for `SQ-12`. This notebook finds such failures, then fixes them by combining the
# vector score with a simple **keyword score**.

# %%
import numpy as np
from sentence_transformers import SentenceTransformer
from labagent.ai.rag.labkit import check, load_docs, load_env, words

load_env()
docs = load_docs()
model = SentenceTransformer("all-MiniLM-L6-v2")

# %%
#### REFERENCE chunk_fixed
#### REFERENCE chunk_paragraphs
#### REFERENCE TinyStore

chunks = [{"id": f"{name}#{i}", "doc": name, "text": text}
          for name, doc_text in docs.items()
          for i, text in enumerate(chunk_paragraphs(doc_text, 500, 50))]
store = TinyStore()
store.add(chunks)

# %% [markdown]
# ## Two questions that go wrong
#
# `SQ-12` is the short security questionnaire for Tier 2 vendors (in `vendor_risk_management`). `72 hours` is the deadline to
# notify a personal data breach (in `incident_response`). Run the cell and see which document comes first.

# %%
def show(hits):
    for h in hits:
        print(f"   {h['score']:.2f}  {h['id']}")

for q, right in [("SQ-12", "vendor_risk_management"), ("72 hours", "incident_response")]:
    top = store.search(q, k=3)
    print(f"{q!r}: should be {right}, vector search says {top[0]['doc']}  {'OK' if top[0]['doc'] == right else 'WRONG'}")
    show(top)

# %% [markdown]
# The vector search found something related to "questionnaires" and "hours", not the passage that contains the exact token.
# The words *are* in the right chunk. A keyword check would see that.
#
# ## Exercise 1: a keyword score
#
# `keyword_score(query, text)` is the fraction of the query's words that appear in the text, from 0 to 1. Use
# `words(text)` (lower-case word list, already provided), drop the stop words below from the query, and compare as sets.
# If no query words are left after dropping stop words, return 0.

# %%
STOP = set("a an the of to is are was were what which who how do does we our for in on and or at it be by with from that this as i can my".split())

def keyword_score(query, text):
    """Fraction of the (non-stop) query words found in the text, 0.0 to 1.0."""
    #### SOLUTION keyword_score
    wanted = [w for w in words(query) if w not in STOP]
    if not wanted:
        return 0.0
    present = set(words(text))
    return sum(w in present for w in wanted) / len(wanted)
    #### STUDENT
    # TODO: query words that are not in STOP; the share of them that appear in words(text)
    return 0.0
    #### END

# %%
check("all words present is 1", keyword_score("SQ-12 questionnaire", "Tier 2 vendors complete the short questionnaire SQ-12.") == 1.0, "count the query words found in the text")
check("none present is 0", keyword_score("SQ-12", "Nothing relevant here.") == 0.0, "no match, no score")
check("half present is 0.5", keyword_score("backup restore", "A backup was made.") == 0.5, "two words, one found")
check("stop words are ignored", keyword_score("what is the SQ-12", "SQ-12") == 1.0, "drop words in STOP before counting")
check("an empty query scores 0", keyword_score("the of", "anything") == 0.0, "avoid dividing by zero")

# %% [markdown]
# ## Exercise 2: combine the two scores
#
# `hybrid_search(query, k=3, alpha=0.3, pool=12)`:
# 1. Get the `pool` best chunks by vector search (`store.search(query, k=pool)`).
# 2. For each, `score = (1 - alpha) * vector_score + alpha * keyword_score(query, text)`.
# 3. Sort by the new score, keep `k`, and return them (with the new `score`, plus `vector` and `keyword` so you can inspect).
#
# `alpha=0` is pure vector search and `alpha=1` is pure keyword. Why a `pool`? The keyword part only **reorders** what the vector
# search already found. Keep that in mind for the experiment at the end.

# %%
def hybrid_search(query, k=3, alpha=0.3, pool=12):
    #### SOLUTION hybrid_search
    candidates = store.search(query, k=pool)
    for c in candidates:
        c["vector"], c["keyword"] = c["score"], keyword_score(query, c["text"])
        c["score"] = (1 - alpha) * c["vector"] + alpha * c["keyword"]
    return sorted(candidates, key=lambda c: c["score"], reverse=True)[:k]
    #### STUDENT
    # TODO: vector search for `pool` candidates; add "vector", "keyword" and the combined "score"; sort; keep k
    return []
    #### END

# %%
got = hybrid_search("SQ-12", k=3)
check("it returns k hits with the extra fields", len(got) == 3 and all({"vector", "keyword", "score"} <= set(h) for h in got), 'add "vector" and "keyword" to each hit')
check("alpha=0 is the same as vector search", [h["id"] for h in hybrid_search("How long do we keep customer records?", 3, alpha=0)] == [h["id"] for h in store.search("How long do we keep customer records?", 3)],
      "with alpha=0 the combined score equals the vector score")
check("SQ-12 now finds the vendor document", got[0]["doc"] == "vendor_risk_management", f"top hit is {got[0]['doc']}")
check("72 hours now finds the incident runbook", hybrid_search("72 hours", 1)[0]["doc"] == "incident_response", "the keyword score should lift the chunk that contains the words")
check("results are best first", all(a["score"] >= b["score"] for a, b in zip(got, got[1:])), "sort descending")

# %% [markdown]
# ## Experiment 1: how much keyword is too much?
#
# Five queries, six values of `alpha`. Each cell says whether the top chunk contains the phrase that answers the question
# (`ok`) or not (`X`), and the last column shows what is expected. Queries 1 to 3 contain exact tokens. Queries 4 and 5 are
# plain language, where meaning should carry the answer.

# %%
tests = [("SQ-12", "SQ-12"), ("72 hours", "72 hours"), ("form for exception for vendor", "VR-7"),
         ("how long to keep backups", "35 days"), ("how fast must we recover", "four hours")]
print(f"{'alpha':>6} | " + " | ".join(f"{q[:20]:20}" for q, _ in tests))
for alpha in (0, 0.1, 0.3, 0.5, 0.7, 1.0):
    marks = ["ok" if must.lower() in hybrid_search(q, 1, alpha=alpha)[0]["text"].lower() else "X" for q, must in tests]
    print(f"{alpha:>6} | " + " | ".join(f"{m:20}" for m in marks))
print("expected phrase:", [must for _, must in tests])

# %% [markdown]
# **Your turn (write your answer):** read the table. Five queries is far too few to choose a weight (notebook 7 fixes that), but
# read what it shows. Which `alpha` would you choose for now? Which queries does a high `alpha` damage,
# and why (look at the words in the question and at what the keyword score rewards)? Which does `alpha=0` get wrong?
#
# *Your answer:*
#
# ## Experiment 2: the pool limit
#
# The reranker only sees the `pool` best vector hits. Shrink the pool and watch the exact-token question.

# %%
for pool in (1, 2, 5, 31):
    top = hybrid_search("form for exception for vendor", 1, alpha=0.5, pool=pool)[0]
    print(f"pool={pool:>2}: top hit {top['id']:28} {'ok' if 'VR-7' in top['text'] else 'WRONG'}")

# %% [markdown]
# If the right chunk is not among the vector candidates, no keyword score can rescue it. A real hybrid system runs a separate
# keyword index (BM25, in a search engine or in Postgres full-text search) and merges the two ranked lists, usually with
# *reciprocal rank fusion*. What you wrote is the honest small version of the idea, and it is what the labagent library does
# too. Know its limit.
#
# ## What to take away
#
# - Vector search finds *related*, keyword search finds *exact*. Documents with codes, names, numbers and acronyms need both.
# - A combined score needs a weight, and the right weight depends on your questions. You pick it by measuring, which is
#   notebook 7.
# - A reranker can only reorder candidates it was given.
#
# **Next:** notebook 7 builds the test that tells you whether any of these choices made things better.
