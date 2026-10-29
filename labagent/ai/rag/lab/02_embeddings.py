# %% [markdown]
# # 2. Embeddings: how a computer measures "means the same"
#
# **Time:** about 20 minutes. **Cost:** nothing, the model runs on your machine. **Needs:** notebook 1 (not its code).
#
# A search that matches words fails when the question and the document use different words. "How long do we keep customer
# records?" and "Customer records are deleted seven years after the account closes" share only two words. An **embedding
# model** turns a text into a list of numbers (a vector) so that texts with similar meaning get vectors that point the same way.
# You will use a small model, `all-MiniLM-L6-v2`, and write the similarity measure yourself.

# %%
import numpy as np
from sentence_transformers import SentenceTransformer
from labagent.ai.rag.labkit import check, load_docs, load_env

load_env()
docs = load_docs()
model = SentenceTransformer("all-MiniLM-L6-v2")      # about 90 MB, downloaded once, then cached

vec = model.encode("Passwords must be at least 14 characters.")
print("type :", type(vec).__name__, vec.dtype)
print("shape:", vec.shape, "<- the model always returns this many numbers, whatever the text length")
print("first eight numbers:", np.round(vec[:8], 3))

# %% [markdown]
# The numbers mean nothing one by one. What matters is the **angle** between two vectors. If two texts mean the same, their
# vectors point the same way and the angle is small.
#
# ## Exercise 1: cosine similarity
#
# Cosine similarity is the cosine of that angle: `dot(a, b) / (length(a) * length(b))`. It is 1 for the same direction, 0 for
# unrelated (at right angles) and -1 for opposite. Use `np.dot` and `np.linalg.norm`.

# %%
def cosine(a, b):
    """Cosine similarity of two 1-D numpy vectors, as a float."""
    #### SOLUTION cosine
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    #### STUDENT
    # TODO: dot product divided by the product of the two lengths
    return 0.0
    #### END

# %%
check("same direction is 1", abs(cosine(np.array([1.0, 2.0]), np.array([2.0, 4.0])) - 1) < 1e-9, "scaling a vector must not change the answer: divide by the lengths")
check("right angle is 0", abs(cosine(np.array([1.0, 0.0]), np.array([0.0, 3.0]))) < 1e-9, "dot product of perpendicular vectors is 0")
check("opposite is -1", abs(cosine(np.array([1.0, 1.0]), np.array([-1.0, -1.0])) + 1) < 1e-9, "the sign comes from the dot product")
check("it returns a float", isinstance(cosine(np.array([1.0, 0.0]), np.array([1.0, 1.0])), float), "wrap the result in float(...)")

# %% [markdown]
# ## Predict, then run
#
# Below are one question and four candidate sentences. **Before you run the cell**, write down the order you expect, best match
# first. Then run it.

# %%
question = "How long do we keep customer records?"
candidates = [
    "Customer records are deleted seven years after the account closes.",
    "How long do we keep the office plants?",
    "Passwords must be at least 14 characters.",
    "We retain client files for several years after the relationship ends.",
]
q_vec = model.encode(question)
for text in candidates:
    print(f"{cosine(q_vec, model.encode(text)):6.3f}  {text}")

# %% [markdown]
# Notice the second candidate: it repeats five of the question's words, but it is about plants. And the fourth shares no
# words with the question at all, yet scores well. That is the point of embeddings, and it is what word matching cannot do.
#
# ## Exercise 2: rank a list of texts
#
# Write `rank(query, texts)`: encode the query and all the texts, score each text against the query with your `cosine`, and
# return a list of `(score, text)` sorted best first. Encode the texts in one call: `model.encode(list_of_texts)` is much
# faster than one call per text.

# %%
def rank(query, texts):
    """[(score, text), ...] sorted from most to least similar to the query."""
    #### SOLUTION rank
    q = model.encode(query)
    vectors = model.encode(list(texts))
    scored = [(cosine(q, v), t) for v, t in zip(vectors, texts)]
    return sorted(scored, key=lambda pair: pair[0], reverse=True)
    #### STUDENT
    # TODO: encode the query, encode all texts in ONE call, score each, sort best first
    return []
    #### END

# %%
mini = ["Secrets such as API keys must be rotated every 90 days.", "The cafeteria opens at noon.",
        "Customer records are kept for seven years.", "Lost devices must be reported within one hour."]
got = rank("How often do we change API keys?", mini)
check("returns one pair per text", len(got) == 4 and all(len(p) == 2 for p in got), "return [(score, text), ...]")
check("sorted best first", [s for s, _ in got] == sorted([s for s, _ in got], reverse=True), "sort with reverse=True")
check("the right sentence is first", got[0][1] == mini[0], f"got {got[0][1]!r}")
check("the cafeteria is not first for a device question", rank("What if my laptop is stolen?", mini)[0][1] == mini[3], "check that you score the texts, not the query against itself")
for s, t in rank("What if my laptop is stolen?", mini):
    print(f"{s:6.3f}  {t}")

# %% [markdown]
# ## Where embeddings are blind
#
# Run this and read the numbers. Then answer the question under it.

# %%
base = "Passwords must be at least 14 characters."
for other in ["Passwords must be at most 14 characters.", "Passwords must be at least 8 characters.", "Passwords must never be shared.", "Bicycles must be locked at night."]:
    print(f"{cosine(model.encode(base), model.encode(other)):6.3f}  {other}")

# %% [markdown]
# **Your turn (write your answer):** the first two sentences contradict the base sentence or change a number. How do they
# score compared with the unrelated bicycle sentence? What does that tell you about using embeddings alone to decide whether a
# document *supports* an answer? (Hint for later notebooks: retrieval finds *related* text. The language model, and you, must
# check that it is *right*.)
#
# *Your answer:*

# %% [markdown]
# ## Things that bite in production
#
# - **The index is tied to the model.** Vectors from different models cannot be compared, even if they have the same length.
#   Changing the embedding model means re-embedding every document.
# - **Model limits.** This model reads at most 256 word pieces; longer text is silently cut. Another reason to chunk.
# - **Language.** This model is English. For other languages pick a multilingual model and test it.
# - **Cost.** Here embedding is free and local. A hosted embedding API charges per token, on every document and every query.
#
# **Next:** notebook 3 stores many vectors and finds the closest ones: your own tiny vector database.
