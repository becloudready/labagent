# %% [markdown]
# # 3. A vector store: find the closest chunks
#
# **Time:** about 25 minutes. **Cost:** nothing. **Needs:** notebooks 1 and 2.
#
# A vector database does three things: it **stores** vectors with their text, it **searches** for the nearest ones to a query
# vector, and it **filters** by metadata. Products add speed, persistence and scale, but the idea is small enough to write in
# a few lines of numpy. You will write it now, then replace it with a real database in notebook 5.

# %%
import time
import numpy as np
from sentence_transformers import SentenceTransformer
from labagent.ai.rag.labkit import check, load_docs, load_env

load_env()
docs = load_docs()
model = SentenceTransformer("all-MiniLM-L6-v2")

# %% [markdown]
# ## Starting point: your chunker
#
# This is a working `chunk_fixed` and `chunk_paragraphs` from notebook 1. If yours passed the checks, you can paste yours
# over this cell.

# %%
#### REFERENCE chunk_fixed
#### REFERENCE chunk_paragraphs

# Every chunk is a dict, so it can carry its source with it. This is what a vector store keeps for each piece.
chunks = [{"id": f"{name}#{i}", "doc": name, "text": text}
          for name, doc_text in docs.items()
          for i, text in enumerate(chunk_paragraphs(doc_text, 500, 50))]
print(len(chunks), "chunks from", len(docs), "documents")
print(chunks[3])

# %% [markdown]
# ## Exercise: `TinyStore`
#
# Write two methods.
#
# - `add(chunks)`: embed the `text` of each chunk **in one call**, with `normalize_embeddings=True` (so each vector has length 1
#   and a dot product *is* the cosine similarity), and append the vectors to `self.vectors`, a 2-D array with one row per chunk.
#   Keep the chunk dicts in `self.chunks` in the same order.
# - `search(query, k=3)`: embed the query the same way, get one score per stored chunk with a single matrix-vector product
#   (`self.vectors @ query_vector`), and return the `k` best as copies of the chunk dict with an added `"score"`, best first.
#   `np.argsort` sorts ascending, so sort the negative scores or reverse.

# %%
class TinyStore:
    def __init__(self):
        self.chunks = []
        self.vectors = np.zeros((0, 384), dtype="float32")

    def __len__(self):
        return len(self.chunks)

    def add(self, chunks):
        #### SOLUTION TinyStore
        vectors = model.encode([c["text"] for c in chunks], normalize_embeddings=True)
        self.vectors = np.vstack([self.vectors, vectors])
        self.chunks.extend(chunks)
        #### STUDENT
        # TODO: embed all the texts in one call, stack the rows onto self.vectors, extend self.chunks
        pass
        #### END

    def search(self, query, k=3):
        #### SOLUTION
        q = model.encode(query, normalize_embeddings=True)
        scores = self.vectors @ q
        best = np.argsort(-scores)[:k]
        return [{**self.chunks[i], "score": float(scores[i])} for i in best]
        #### STUDENT
        # TODO: embed the query, one matrix-vector product, take the k best, return copies of the chunks with "score"
        return []
        #### END

# %%
store = TinyStore()
store.add(chunks)
check("every chunk is stored", len(store) == len(chunks) and store.vectors.shape == (len(chunks), 384), f"vectors shape is {store.vectors.shape}, expected ({len(chunks)}, 384)")
hits = store.search("How long do we keep customer records?", k=3)
check("search returns k results", len(hits) == 3, "return the k best")
check("results are best first", hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"], "sort by score, highest first")
check("results are chunk dicts with a score", all({"id", "doc", "text", "score"} <= set(h) for h in hits), 'copy the chunk and add "score"')
check("the retention rule is found", hits[0]["doc"] == "data_handling", f"top hit was {hits[0]['doc']}")
check("scores are cosine similarities", all(-1.0001 <= h["score"] <= 1.0001 for h in hits), "did you pass normalize_embeddings=True for both chunks and query?")
check("the original chunk is not modified", "score" not in chunks[0], "build a new dict instead of changing the stored one")

# %% [markdown]
# ## Try it
#
# Six questions. For each, read the top hit and decide: does it contain the answer? The last one has no answer in the
# documents. Look at its score.

# %%
questions = [
    "How long do we keep customer records?",
    "Who approves a 3,000 dollar expense?",
    "What do I do if my laptop is stolen?",
    "Can I work from Spain?",
    "How often are restore tests run?",
    "What is the lunch menu?",
]
for q in questions:
    top = store.search(q, k=1)[0]
    print(f"{top['score']:.2f}  {q}\n      -> {top['id']}: {top['text'][:110]!r}\n")

# %% [markdown]
# **Your turn (write your answer):** which of the six top hits does not actually answer the question? What was the score of the
# one with no answer in the documents, compared with the scores of the answerable ones? Keep that observation, because in
# notebook 8 you will turn it into a rule.
#
# *Your answer:*

# %% [markdown]
# ## Why "brute force" works here, and when it stops
#
# Your search compares the query to every vector. That is exact and simple. How slow is it as the collection grows? This
# times the same search over random vectors of the same size.

# %%
rng = np.random.default_rng(0)
q = rng.normal(size=384).astype("float32")
for n in (1_000, 10_000, 100_000):
    matrix = rng.normal(size=(n, 384)).astype("float32")
    t = time.perf_counter()
    top = np.argsort(-(matrix @ q))[:3]
    ms = (time.perf_counter() - t) * 1000
    print(f"{n:>9,} vectors: {ms:8.1f} ms   ({matrix.nbytes / 1e6:6.0f} MB of memory)")
    del matrix
print(f"1,000,000 vectors: about {ms * 10:.0f} ms and {100_000 * 384 * 4 * 10 / 1e6:.0f} MB (estimated: ten times the last line, not measured)")

# %% [markdown]
# Brute force is fine for tens of thousands of chunks and starts to hurt around a million, in time and above all in memory. Beyond that, databases use an
# **approximate** index (HNSW is the common one): they check a clever subset of vectors, answer in milliseconds, and
# sometimes miss the true nearest neighbour. That is the real trade a vector database makes, and it is why "accuracy"
# for a vector database has a different meaning than for a normal one.
#
# ## What to take away
#
# - A vector store is stored vectors, plus their text and metadata, plus a nearest-neighbour search.
# - Normalised vectors make the dot product equal to cosine similarity, which is one fast matrix product.
# - The score is not a probability. It tells you how close, not how right. Notice how the unanswerable question scored.
#
# **Next:** notebook 4 gives the retrieved chunks to a language model and makes it answer from them.
