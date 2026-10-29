# %% [markdown]
# # 7. Evaluation: know whether a change helped
#
# **Time:** about 30 minutes. **Cost:** nothing. **Needs:** notebooks 1 to 6.
#
# So far you judged results by eye on a few questions. That cannot tell you whether a new chunk size, a different `alpha`, or a
# new embedding model is better; and a RAG system that nobody measures gets quietly worse. You will build the smallest useful
# test: a list of questions with a known answer phrase, a score, and a table that compares settings.
#
# We test **retrieval** only, because it is cheap, fast and repeatable: did the answer-bearing text reach the top `k` results?
# If retrieval fails, no prompt can save the answer. (Testing the generated answers is also needed, and is a harder problem. See
# the end.)

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
#### REFERENCE keyword_score

def norm(text):
    """Lower-case and collapse all whitespace, so a phrase still matches when the document wraps the line in the middle of it."""
    return " ".join(text.lower().split())

def build_store(size, overlap=50):
    chunks = [{"id": f"{name}#{i}", "doc": name, "text": text}
              for name, doc_text in docs.items()
              for i, text in enumerate(chunk_paragraphs(doc_text, size, overlap))]
    s = TinyStore()
    s.add(chunks)
    return s

def make_search(size, alpha=0.0, pool=12):
    """A function search(question, k) over a store with this chunk size and keyword weight."""
    s = build_store(size)
    def search(question, k):
        hits = s.search(question, k=max(pool, k))
        for h in hits:
            h["score"] = (1 - alpha) * h["score"] + alpha * keyword_score(question, h["text"])
        return sorted(hits, key=lambda h: h["score"], reverse=True)[:k]
    return search

# %% [markdown]
# ## The test set
#
# Each case is a question and a phrase (`must`) that appears in the text that answers it. A retrieval **hit** means the phrase
# is in one of the top `k` chunks (compared with the helper `norm`, which ignores case and line breaks). The first twelve use wording close to the documents; the last eight are paraphrases, which are harder. All were written by reading the documents and asking what a colleague would ask.

# %%
CASES = [
    {"question": "How long do we keep customer records after an account closes?", "must": "seven years"},
    {"question": "How often must secrets be rotated?", "must": "90 days"},
    {"question": "How many characters must a password have?", "must": "14 characters"},
    {"question": "How soon must a personal data breach be notified?", "must": "72 hours"},
    {"question": "What is the monthly home internet stipend?", "must": "50 dollars"},
    {"question": "Which form is needed to work from another country?", "must": "HR-12"},
    {"question": "How quickly must a vendor report a breach?", "must": "48 hours"},
    {"question": "What is the recovery time objective for Tier 1 systems?", "must": "four hours"},
    {"question": "Who must approve an expense of 3,000 dollars?", "must": "department head"},
    {"question": "How long are internal assistant prompts logged?", "must": "30 days"},
    {"question": "Can customer records be entered into an AI tool?", "must": "never be entered"},
    {"question": "How often are restore tests run for Tier 1 systems?", "must": "every quarter"},
    {"question": "How many days a year can I work from a foreign country?", "must": "30 calendar days"},
    {"question": "Can I use a free chatbot for work?", "must": "not approved"},
    {"question": "What do I do if I lose my phone or laptop?", "must": "within one hour"},
    {"question": "How long can an exception to the vendor rules last?", "must": "six months"},
    {"question": "What proves that a backup actually works?", "must": "never been restored"},
    {"question": "Do I need receipts for small purchases?", "must": "above 25 dollars"},
    {"question": "Is there a cap on hotel prices?", "must": "220 dollars"},
    {"question": "How soon must leadership hear about a serious incident?", "must": "every hour"},
]
print(len(CASES), "cases; every phrase is in the documents:", all(any(norm(c["must"]) in norm(t) for t in docs.values()) for c in CASES))

# %% [markdown]
# ## Exercise 1: score a search function
#
# `evaluate(search, cases, k)` runs `search(question, k)` for each case and returns a dict with
# - `hit_rate`: the share of cases where **any** of the first `k` hits contains `must` (compare `norm(must)` with `norm(hit['text'])`),
# - `mrr` (mean reciprocal rank): for each case, `1 / rank` of the first hit that contains `must` (0 if none), averaged. It
#   rewards putting the right chunk first: rank 1 scores 1, rank 2 scores 0.5, rank 3 scores 0.33.
# - `misses`: the list of questions that failed.
#
# Only look at the first `k` hits, even if `search` returns more.

# %%
def evaluate(search, cases, k=3):
    #### SOLUTION evaluate
    hits_found, reciprocal, misses = 0, 0.0, []
    for case in cases:
        results = search(case["question"], k)[:k]
        ranks = [i for i, h in enumerate(results, 1) if norm(case["must"]) in norm(h["text"])]
        if ranks:
            hits_found += 1
            reciprocal += 1 / ranks[0]
        else:
            misses.append(case["question"])
    return {"hit_rate": hits_found / len(cases), "mrr": reciprocal / len(cases), "misses": misses}
    #### STUDENT
    # TODO: loop over cases; search; keep only the first k; find the rank of the first hit containing must (compare norm(...) of both)
    return {"hit_rate": 0.0, "mrr": 0.0, "misses": []}
    #### END

# %% [markdown]
# **Check 1** tests your scorer with made-up search functions, so you know the scorer is right before you trust it.

# %%
two = [{"question": "q1", "must": "alpha"}, {"question": "q2", "must": "beta"}]
perfect = lambda q, k: [{"text": {"q1": "has alpha in it", "q2": "has BETA in it"}[q]}]
nothing = lambda q, k: []
second = lambda q, k: [{"text": "irrelevant"}, {"text": {"q1": "alpha", "q2": "beta"}[q]}]
third = lambda q, k: [{"text": "x"}, {"text": "y"}, {"text": {"q1": "alpha", "q2": "beta"}[q]}]
check("a perfect search scores 1", evaluate(perfect, two, 3)["hit_rate"] == 1 and evaluate(perfect, two, 3)["mrr"] == 1, "rank 1 gives 1/1")
check("matching ignores upper and lower case", evaluate(perfect, two, 3)["misses"] == [], "compare norm(must) with norm(text)")
check("a search that finds nothing scores 0", evaluate(nothing, two, 3) == {"hit_rate": 0.0, "mrr": 0.0, "misses": ["q1", "q2"]}, "list every failed question in misses")
check("rank 2 gives 0.5", evaluate(second, two, 3)["mrr"] == 0.5 and evaluate(second, two, 3)["hit_rate"] == 1, "reciprocal rank is 1 / rank")
check("only the first k hits count", evaluate(third, two, 2)["hit_rate"] == 0 and evaluate(third, two, 3)["hit_rate"] == 1, "slice results[:k]")

# %% [markdown]
# ## Run it on your pipeline
#
# One search function, one score. We look at `k=1` first: is the answer in the single best chunk? That is the strictest test,
# and the one that shows differences.

# %%
baseline = make_search(size=500, alpha=0.0)
r = evaluate(baseline, CASES, k=1)
print(f"top-1 hit rate {r['hit_rate']:.2f}   (looking at only the single best chunk)")
for m in r["misses"]:
    print("MISS:", m)

# %% [markdown]
# Read the misses. For each one, open the document, find the sentence that answers it, and decide: is the information in a
# chunk that was never retrieved (a retrieval problem), or split across two chunks (a chunking problem), or phrased so
# differently from the question that the embedding missed it (a model problem)?
#
# **Your turn (write your answer):** for each miss, which of the three is it?
#
# *Your answer:*
#
# ## Exercise 2: compare settings
#
# Run the grid: five chunk sizes, with and without the keyword weight. This takes a minute, because every setting builds its
# own store. Fill the table, then read it.

# %%
print(f"{'chunk size':>10} {'alpha':>6} {'hit@1':>6} {'hit@3':>6} {'mrr@3':>6}")
grid = {}
for size in (150, 300, 500, 1000, 3000):
    for alpha in (0.0, 0.2):
        search = make_search(size, alpha)
        r1, r3 = evaluate(search, CASES, k=1), evaluate(search, CASES, k=3)
        grid[(size, alpha)] = (r1, r3)
        print(f"{size:>10} {alpha:>6} {r1['hit_rate']:>6.2f} {r3['hit_rate']:>6.2f} {r3['mrr']:>6.2f}")

# %% [markdown]
# Now the honest part. There are 20 cases, so **one case is worth 5 points** of hit rate. A difference of one case between two
# settings is noise, not evidence. Before you believe a result you need more cases, and cases that were not written to flatter the
# setting you like. State what the table does and does not support, in your answer below.
#
# **Your turn (write your answer):** which setting would you ship, and how sure are you?
#
# *Your answer:*
#
# ## Exercise 3: add your own cases
#
# Add at least three cases of your own to `MY_CASES`. Read a document, pick a fact, write the question a colleague would ask,
# and copy the exact phrase that answers it. The check makes sure your phrase really is in the documents, because a test with
# a wrong answer key is worse than no test.

# %%
MY_CASES = [
    #### SOLUTION
    {"question": "Who has to sign a vendor exception?", "must": "Chief Information Security Officer"},
    {"question": "How long is the new furniture allowance?", "must": "400 dollars"},
    {"question": "When are expense claims paid?", "must": "the 15th"},
    #### STUDENT
    # {"question": "...", "must": "..."},
    #### END
]

# %%
check("you wrote at least three cases", len(MY_CASES) >= 3, "add three dicts with 'question' and 'must'")
check("every question is a real question", all(c["question"].strip().endswith("?") and len(c["question"]) > 12 for c in MY_CASES), "end with a question mark")
check("every phrase is in the documents", all(any(norm(c["must"]) in norm(t) for t in docs.values()) for c in MY_CASES),
      "copy the phrase exactly from a document: " + str([c["must"] for c in MY_CASES if not any(norm(c["must"]) in norm(t) for t in docs.values())]))
combined = CASES + MY_CASES
best = make_search(500, 0.2)
r = evaluate(best, combined, k=1)
print(f"with your cases: {len(combined)} cases, top-1 hit rate {r['hit_rate']:.2f}")
for m in r["misses"]:
    print("MISS:", m)

# %% [markdown]
# ## What this does not measure
#
# - **Answer quality.** The right chunk can reach the model and the answer can still be wrong. Scoring answers needs either
#   reference answers and a human or a model as judge. A model judge is cheap and biased, so check it against people.
# - **Questions people actually ask.** Your cases came from you. Collect real questions, including the ones that failed.
# - **Absent answers.** Add cases whose correct result is "not in the documents" (notebook 8).
#
# ## What to take away
#
# - You cannot improve what you do not measure. Twelve cases and a loop are enough to start.
# - Check the scorer, then the data, then the system. Wrong answer keys are the most common error.
# - Small test sets give noisy comparisons. Say how sure you are.
#
# **Next:** notebook 8 attacks your system with the messy things real document collections contain.
