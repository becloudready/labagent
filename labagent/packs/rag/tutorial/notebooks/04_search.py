# Databricks notebook source
# MAGIC %md
# MAGIC # 04. Search: indexes, keywords, hybrid and filters
# MAGIC
# MAGIC **Time:** about 15 minutes.
# MAGIC
# MAGIC Vector search finds passages with similar **meaning**. It is weak at exact wording, such as a product code or a phrase. Keyword search is the opposite. In this notebook you add indexes, run both kinds of search, merge them, and narrow results with filters.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]>=3.1" --quiet

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Add three indexes
# MAGIC
# MAGIC Without an index every search reads the whole table. With 1,880 rows that is fast anyway, but a real system has millions.
# MAGIC
# MAGIC | Index | Speeds up | Trade-off |
# MAGIC |---|---|---|
# MAGIC | **HNSW** on `embedding` | Nearest-vector search | Approximate: it may miss a true neighbour. Takes time and memory to build |
# MAGIC | **GIN** on `tsv` | Keyword search | Slower inserts |
# MAGIC | **GIN** on `meta` | Metadata filters (`meta @> ...`) | Slower inserts |

# COMMAND ----------

with pg() as cx:
    cx.execute(f"CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON {SCHEMA}.chunks USING hnsw (embedding vector_cosine_ops)")
    cx.execute(f"CREATE INDEX IF NOT EXISTS chunks_tsv ON {SCHEMA}.chunks USING gin (tsv)")
    cx.execute(f"CREATE INDEX IF NOT EXISTS chunks_meta ON {SCHEMA}.chunks USING gin (meta jsonb_path_ops)")
    for name, ddl in cx.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s ORDER BY 1", (SCHEMA,)).fetchall():
        print(name)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Two kinds of search, side by side
# MAGIC
# MAGIC The question is an exact phrase from Apple's filing. The meaning is generic, so vectors alone struggle. Run both and compare the first column.

# COMMAND ----------

def show(rows):
    for n, r in enumerate(rows, 1):
        print(f"  #{n}  {r[0]:5} {r[1]}  {r[2][:45]}")

phrase = "52- or 53-week fiscal year"
qv = vec(embed([phrase])[0])

with pg() as cx:
    print("VECTOR search (meaning):")
    show(cx.execute(f"SELECT meta->>'ticker', meta->>'fiscal_year', meta->>'section' FROM {SCHEMA}.chunks ORDER BY embedding <=> %s::vector LIMIT 5", (qv,)).fetchall())

    print("\nKEYWORD search (exact words):")
    show(cx.execute(f"SELECT meta->>'ticker', meta->>'fiscal_year', meta->>'section' FROM {SCHEMA}.chunks, websearch_to_tsquery('english', %s) q "
                    "WHERE tsv @@ q ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 5", (phrase,)).fetchall())

# COMMAND ----------

# MAGIC %md
# MAGIC Vector search may rank another company first, because the phrase is generic. Keyword search finds the chunks that contain those exact words. Neither is enough alone.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Merge them: reciprocal rank fusion
# MAGIC
# MAGIC Run both searches, then score each passage by where it ranked in each list: `1 / (60 + rank)`. A passage near the top of **both** lists wins. The constant 60 stops one first place from dominating.

# COMMAND ----------

# a worked example with three passages A, B, C
vector_order = ["A", "B", "C"]
keyword_order = ["C", "A"]
scores = {}
for order in (vector_order, keyword_order):
    for rank, item in enumerate(order, 1):
        scores[item] = scores.get(item, 0) + 1 / (60 + rank)
print({k: round(v, 5) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])})
print("A is first in the vector list and second in the keyword list, so it beats C, which is first only in the keyword list.")

# COMMAND ----------

# MAGIC %md
# MAGIC The helper `hybrid_search` in `_common` does this against your table. Use it on the same phrase.

# COMMAND ----------

with pg() as cx:
    hybrid = hybrid_search(cx, embed([phrase])[0], phrase, k=5)
for n, r in enumerate(hybrid, 1):
    print(f"  #{n}  {r['meta']['ticker']:5} {r['meta']['fiscal_year']}  {r['meta']['section'][:45]}   score {r['score']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Filters
# MAGIC
# MAGIC When you already know the company, a filter is exact: no other company can appear. The filter is part of the same SQL query as the similarity search.

# COMMAND ----------

question = "What are the main supply chain risks?"
qvec = embed([question])[0]

with pg() as cx:
    print("no filter:")
    for r in hybrid_search(cx, qvec, question, k=5):
        print("  ", r["meta"]["ticker"], r["meta"]["fiscal_year"], r["meta"]["section"][:40])

    print("\nonly Walmart:")
    for r in hybrid_search(cx, qvec, question, k=5, filters={"ticker": "WMT"}):
        print("  ", r["meta"]["ticker"], r["meta"]["fiscal_year"], r["meta"]["section"][:40])

    print("\nonly Walmart, fiscal year 2025:")
    for r in hybrid_search(cx, qvec, question, k=5, filters={"ticker": "WMT", "fiscal_year": "2025"}):
        print("  ", r["meta"]["ticker"], r["meta"]["fiscal_year"], r["meta"]["section"][:40])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Optional: ask the database how it ran the search

# COMMAND ----------

with pg() as cx:
    plan = cx.execute(f"EXPLAIN SELECT id FROM {SCHEMA}.chunks ORDER BY embedding <=> %s::vector LIMIT 5", (qv,)).fetchall()
print("\n".join(p[0] for p in plan))
print("\nOn a table this small Postgres may choose to read every row instead of using the HNSW index. That is normal: the index pays off at scale.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

with pg() as cx:
    names = {r[0] for r in cx.execute("SELECT indexname FROM pg_indexes WHERE schemaname = %s", (SCHEMA,)).fetchall()}
check({"chunks_embedding_hnsw", "chunks_tsv", "chunks_meta"} <= names, "the three indexes exist")
check(any(r["meta"]["ticker"] == "AAPL" for r in hybrid[:3]), "hybrid search puts an Apple chunk in the top 3 for the Apple phrase")
check(all(r["meta"]["ticker"] == "WMT" for r in hybrid_search(pg(), qvec, question, k=5, filters={"ticker": "WMT"})), "a filter returns only the filtered company")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What to notice
# MAGIC
# MAGIC - **Hybrid search** is the default choice when users ask for exact terms and for ideas.
# MAGIC - **Filters** are the cheapest way to improve precision when the question names a company, a year or a department.
# MAGIC - Every extra index makes writes slower. Add the ones your queries need.
# MAGIC
# MAGIC **Your turn:** try `iPhone 17 Pro` and `Permian Basin`. Does hybrid change the order compared with vectors alone? Sometimes it does not, and that is worth knowing.
# MAGIC
# MAGIC **Next:** `05_answer_and_eval`
