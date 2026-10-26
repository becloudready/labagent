# Databricks notebook source
# MAGIC %md
# MAGIC # 02. Embeddings: turning meaning into numbers
# MAGIC
# MAGIC **Time:** about 10 minutes.
# MAGIC
# MAGIC An **embedding** is a list of numbers that captures the meaning of a text. Texts with similar meaning get vectors that point in similar directions. Searching means finding the stored vectors closest to the vector of your question.
# MAGIC
# MAGIC In this notebook you call the embedding model directly, so you see the raw request before any helper hides it.

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. One request to the embedding model
# MAGIC
# MAGIC The model is a serving endpoint. You send text, you get a vector back.

# COMMAND ----------

sentences = ["Passwords must be at least 14 characters", "Use a long password", "The cafeteria opens at noon"]

resp = w.api_client.do("POST", f"/serving-endpoints/{EMBED_ENDPOINT}/invocations", body={"input": sentences})
vectors = [d["embedding"] for d in resp["data"]]

print("endpoint:           ", EMBED_ENDPOINT)
print("vectors returned:   ", len(vectors))
print("numbers per vector: ", len(vectors[0]))
print("first 6 numbers:    ", [round(x, 4) for x in vectors[0][:6]])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Which sentences are close?
# MAGIC
# MAGIC **Cosine similarity** measures the angle between two vectors: 1.0 is the same direction, 0 is unrelated.

# COMMAND ----------

def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5))

print("password rule    vs  long password   :", round(cosine(vectors[0], vectors[1]), 3))
print("password rule    vs  cafeteria hours :", round(cosine(vectors[0], vectors[2]), 3))

# COMMAND ----------

# MAGIC %md
# MAGIC The first pair shares **meaning** but almost no words, and it scores far higher than the unrelated pair. That is what keyword search cannot do.
# MAGIC
# MAGIC **Your turn:** add your own sentences to the list above and predict the scores before you run it.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Embed real chunks, politely
# MAGIC
# MAGIC The endpoint limits how fast you can call it. The `embed()` helper in `_common` sends **8 texts per request** and waits and retries if the endpoint says to slow down. Time a small batch.

# COMMAND ----------

rows = load_rows()
sample = [f"{r['title']}\n{r['content']}" for r in rows[:16]]

t = time.time()
sample_vectors = embed(sample)
print(f"embedded {len(sample_vectors)} chunks in {time.time() - t:.1f}s ({(time.time() - t) / len(sample):.2f}s each)")
print(f"embedding all {len(rows)} chunks will take roughly {len(rows) * (time.time() - t) / len(sample) / 60:.0f} minutes")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

check(len(vectors[0]) == 1024, "each vector has 1,024 numbers (this model's dimension)")
check(cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2]), "similar meaning scores higher than unrelated text")
check(len(sample_vectors) == 16, "16 chunks embedded")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What to notice
# MAGIC
# MAGIC - The vector has **1,024 numbers**. The database column in the next notebook must be declared with exactly that size.
# MAGIC - Embedding is **the slow step** of building a RAG system. You pay it once per chunk, and again whenever a chunk changes.
# MAGIC - The model sees at most about 512 tokens, so `embed()` cuts each text at 1,800 characters.
# MAGIC
# MAGIC **Next:** `03_pgvector`
