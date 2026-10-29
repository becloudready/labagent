# %% [markdown]
# # 5. A real vector database, and metadata filters
#
# **Time:** about 20 minutes. **Cost:** nothing. **Needs:** notebooks 1 to 4.
#
# Your `TinyStore` works. What does a vector database give you that it does not? Persistence on disk, an approximate index that
# scales, and **filters**: "only search the finance documents". You will write a `ChromaStore` with the same two methods as
# `TinyStore`, so the rest of your pipeline does not change, and then compare the two. Chroma is a free, embedded database
# (no server) that is a good fit for a laptop lab.

# %%
import chromadb
import numpy as np
from sentence_transformers import SentenceTransformer
from labagent.ai.rag.labkit import check, lab_root, load_docs, load_env

load_env()
docs = load_docs()
model = SentenceTransformer("all-MiniLM-L6-v2")

# %%
#### REFERENCE chunk_fixed
#### REFERENCE chunk_paragraphs
#### REFERENCE TinyStore

# Metadata: which department owns each document. A real system gets this from the document source, not from code.
DEPT = {"security_policy": "it", "data_handling": "it", "incident_response": "it", "ai_usage_guidelines": "it",
        "backup_and_recovery": "it", "vendor_risk_management": "legal", "remote_work_policy": "hr", "expense_policy": "finance"}
chunks = [{"id": f"{name}#{i}", "doc": name, "dept": DEPT[name], "text": text}
          for name, doc_text in docs.items()
          for i, text in enumerate(chunk_paragraphs(doc_text, 500, 50))]
tiny = TinyStore()
tiny.add(chunks)
print(len(tiny), "chunks;", sorted(set(DEPT.values())), "departments")

# %% [markdown]
# ## Exercise: `ChromaStore`
#
# Chroma stores, per item: an `id`, a vector (`embeddings`), the original text (`documents`) and a metadata dict (`metadatas`).
#
# - `__init__` creates a **cosine** collection: `client.create_collection(name, metadata={"hnsw:space": "cosine"})`. Chroma
#   keeps state in the process, so delete any collection with that name first (`client.delete_collection(name)` raises if it
#   does not exist: wrap it in `try/except Exception`).
# - `add(chunks)`: compute the vectors yourself with the same model and `normalize_embeddings=True`, and call `collection.add`.
#   Put `doc` and `dept` in the metadata.
# - `search(query, k=3, where=None)`: `collection.query(query_embeddings=[vector], n_results=k, where=where)`. Chroma returns
#   *distances*; for cosine, similarity is `1 - distance`. Return the same shape as `TinyStore.search`: dicts with `id`,
#   `doc`, `dept`, `text` and `score`.
#
# A filter is a dict: `{"dept": "finance"}`. For several values use `{"dept": {"$in": ["hr", "finance"]}}`.

# %%
class ChromaStore:
    def __init__(self, name="acme_lab"):
        #### SOLUTION ChromaStore
        self.client = chromadb.EphemeralClient()
        try:
            self.client.delete_collection(name)
        except Exception:
            pass
        self.col = self.client.create_collection(name, metadata={"hnsw:space": "cosine"})
        #### STUDENT
        # TODO: create self.client (chromadb.EphemeralClient()) and self.col, a cosine collection, after deleting an old one
        self.col = None
        #### END

    def __len__(self):
        return self.col.count() if self.col is not None else 0

    def add(self, chunks):
        #### SOLUTION
        vectors = model.encode([c["text"] for c in chunks], normalize_embeddings=True)
        self.col.add(ids=[c["id"] for c in chunks], documents=[c["text"] for c in chunks], embeddings=vectors.tolist(),
                     metadatas=[{"doc": c["doc"], "dept": c["dept"]} for c in chunks])
        #### STUDENT
        # TODO: embed the texts, then self.col.add(ids=..., documents=..., embeddings=..., metadatas=...)
        pass
        #### END

    def search(self, query, k=3, where=None):
        #### SOLUTION
        q = model.encode(query, normalize_embeddings=True)
        r = self.col.query(query_embeddings=[q.tolist()], n_results=k, where=where)
        return [{"id": i, "text": t, "doc": m["doc"], "dept": m["dept"], "score": 1 - d}
                for i, t, m, d in zip(r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0])]
        #### STUDENT
        # TODO: embed the query, call self.col.query(..., where=where), turn distance into score = 1 - distance
        return []
        #### END

# %%
chroma = ChromaStore()
chroma.add(chunks)
check("all chunks are in the database", len(chroma) == len(chunks), f"stored {len(chroma)} of {len(chunks)}")
for q in ["How long do we keep customer records?", "Who approves a 3,000 dollar expense?", "What if my laptop is stolen?", "Can I work from Spain?"]:
    a, b = tiny.search(q, 1)[0], chroma.search(q, 1)[0]
    check(f"same top hit as TinyStore: {q}", a["id"] == b["id"], f"TinyStore says {a['id']}, Chroma says {b['id']}: is the space cosine and the vector normalised?")
    check("  and almost the same score", abs(a["score"] - b["score"]) < 0.01, f"{a['score']:.3f} vs {b['score']:.3f}: score = 1 - distance")
hits = chroma.search("What are the rules for money and travel?", k=5, where={"dept": "finance"})
check("a filter keeps only the finance documents", hits and all(h["dept"] == "finance" for h in hits), "pass where= to the query")
two = chroma.search("What are the rules for people working remotely?", k=8, where={"dept": {"$in": ["hr", "finance"]}})
check("$in matches several departments", {h["dept"] for h in two} <= {"hr", "finance"} and len(two) >= 2, 'use {"dept": {"$in": [...]}}')

# %% [markdown]
# ## What a filter is for
#
# Run the same question with and without a filter.

# %%
q = "How many days notice do I need to give?"
print("no filter:")
for h in chroma.search(q, k=4):
    print(f"  {h['score']:.2f}  {h['dept']:8} {h['id']}")
print("hr only:")
for h in chroma.search(q, k=4, where={"dept": "hr"}):
    print(f"  {h['score']:.2f}  {h['dept']:8} {h['id']}")

# %% [markdown]
# **Your turn (write your answer):** a filter is an exact rule applied *before* ranking. Give two real situations where you
# would filter (think about who is asking, and about which version of a document is current), and one where a filter would
# hide the document the user needed. Who decides the metadata, and what happens when it is wrong or missing?
#
# *Your answer:*

# %% [markdown]
# ## Everything you wrote is inside the library
#
# `RAGLabClient` is the labagent library's version of the same pipeline: it chunks, embeds, stores in Chroma, retrieves (with a
# small keyword rerank like notebook 6) and calls the model. The numbers differ from yours on purpose: its chunks are
# about 800 characters instead of your 500, and its score includes the keyword part. Same ideas, different settings.

# %%
from labagent.ai.rag import RAGLabClient

lib = RAGLabClient(vector_db="chroma", llm_provider="mock")
res = lib.ingest(str(lab_root() / "data" / "sample_docs"))
print(res.documents, "documents,", res.chunks, "chunks,", res.embedding_model)
for c in lib.retrieve("How long do we keep customer records?", top_k=2):
    print(f"library : {c.similarity:.2f} {c.doc_id}")
for h in chroma.search("How long do we keep customer records?", k=2):
    print(f"yours   : {h['score']:.2f} {h['doc']}")

# %% [markdown]
# ## What to take away
#
# - A vector database stores the same four things you stored (id, vector, text, metadata) and adds an index, persistence and
#   filtering. The interface you wrote is the interface they all have.
# - Use a filter when you know something exact about the question (who asks, which product, which year). Use similarity for the
#   rest. They combine.
# - Metadata is part of your data quality. A wrong department tag hides a document as surely as deleting it.
#
# **Next:** notebook 6 shows a kind of question where meaning-based search fails, and adds the fix.
