# Databricks notebook source
# MAGIC %md
# MAGIC # 03. pgvector: store vectors and search them
# MAGIC
# MAGIC **Time:** about 15 minutes (most of it is embedding the filings).
# MAGIC
# MAGIC You will connect to the Lakebase database, create a table with a vector column, and load the filings in **two stages on purpose**. First every company except Apple. You then ask about Apple and see what search does when the right document was never embedded. Then you load Apple and ask again.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]>=3.1" --quiet

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Connect to Lakebase
# MAGIC
# MAGIC There is no stored password. You ask Databricks for a token and use it as the Postgres password. The `pg()` helper in `_common` does exactly the two steps below.

# COMMAND ----------

import psycopg

token = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": LAKEBASE_ENDPOINT})["token"]
conn = psycopg.connect(host=lakebase_host(), port=5432, dbname="databricks_postgres", user=ME, password=token,
                       sslmode="require", autocommit=True, connect_timeout=30)
print(conn.execute("select version()").fetchone()[0][:60])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create the table
# MAGIC
# MAGIC `CREATE EXTENSION vector` turns on pgvector. The `embedding` column is declared `vector(1024)`, the size you saw in notebook 02. The `tsv` column is a generated full-text index of the content: you will use it for keyword search in notebook 04.

# COMMAND ----------

DIM = len(embed(["dimension probe"])[0])
print("embedding dimension:", DIM)

with pg() as cx:
    cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cx.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    cx.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.chunks (
                     id text PRIMARY KEY, title text, content text, meta jsonb,
                     embedding vector({DIM}),
                     tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED)""")
    print("rows in the table now:", cx.execute(f"SELECT count(*) FROM {SCHEMA}.chunks").fetchone()[0])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. A loader: embed, then insert
# MAGIC
# MAGIC For each batch of chunks, embed their text and insert the chunk with its vector. `ON CONFLICT ... DO UPDATE` makes it safe to run twice: a chunk that is already there is replaced, not duplicated.

# COMMAND ----------

def load_chunks(chunks):
    started = time.time()
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        vectors = embed([f"{c['title']}\n{c['content']}" for c in batch])
        with pg() as cx:
            for c, v in zip(batch, vectors):
                cx.execute(f"INSERT INTO {SCHEMA}.chunks (id, title, content, meta, embedding) VALUES (%s, %s, %s, %s::jsonb, %s::vector) "
                           "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, content = EXCLUDED.content, "
                           "meta = EXCLUDED.meta, embedding = EXCLUDED.embedding",
                           (c["id"], c["title"], c["content"], json.dumps(c["meta"]), vec(v)))
        if (i // BATCH) % 25 == 0:
            print(f"  {min(i + BATCH, len(chunks))}/{len(chunks)} chunks embedded and stored ({time.time() - started:.0f}s)")
    print(f"done: {len(chunks)} chunks in {time.time() - started:.0f}s")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Stage one: load everyone except Apple
# MAGIC
# MAGIC The Apple filings stay in Python memory. They are **not** embedded and **not** in the database.

# COMMAND ----------

rows = load_rows()
first = [r for r in rows if r["meta"]["ticker"] != "AAPL"]
later = [r for r in rows if r["meta"]["ticker"] == "AAPL"]
print(f"loading {len(first)} chunks now; holding back {len(later)} Apple chunks")

load_chunks(first)

# COMMAND ----------

with pg() as cx:
    print(cx.execute(f"SELECT meta->>'ticker', count(*) FROM {SCHEMA}.chunks GROUP BY 1 ORDER BY 1").fetchall())

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Ask about Apple
# MAGIC
# MAGIC This is a plain **vector search**: embed the question, then order the table by distance to it. `<=>` is pgvector's cosine distance, so `1 - distance` is the similarity. `meta @> '{"ticker": "AAPL"}'` is a metadata filter.

# COMMAND ----------

def vector_search(question, ticker=None, k=5):
    qv = vec(embed([question])[0])
    where, args = ("WHERE meta @> %s::jsonb", [json.dumps({"ticker": ticker})]) if ticker else ("", [])
    with pg() as cx:
        return cx.execute(f"SELECT meta->>'ticker', meta->>'fiscal_year', meta->>'section', round((1 - (embedding <=> %s::vector))::numeric, 3) AS similarity "
                          f"FROM {SCHEMA}.chunks {where} ORDER BY embedding <=> %s::vector LIMIT {int(k)}", (qv, *args, qv)).fetchall()

question = "What are Apple's main supply chain and manufacturing risks?"

print("Search only Apple (filter):")
only_apple = vector_search(question, ticker="AAPL")
print("  rows found:", len(only_apple))

print("\nSearch everything:")
everything = vector_search(question)
for r in everything:
    print("  ", r)
before = {"best": max(float(r[3]) for r in everything), "companies": sorted({r[0] for r in everything})}

# COMMAND ----------

# MAGIC %md
# MAGIC **What to notice**
# MAGIC
# MAGIC 1. The Apple-only search finds **nothing**. There are no Apple vectors to search.
# MAGIC 2. The search over everything still returns rows. They are other companies' chunks, the closest of what exists, with lower similarity. **Search never says "I have nothing."** It returns the nearest thing it has.
# MAGIC
# MAGIC The Apple data is not wrong. It was never embedded. When a RAG system answers badly, check that the document is in the index before blaming the model.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Stage two: load Apple, ask again

# COMMAND ----------

load_chunks(later)

# COMMAND ----------

print("Search only Apple (filter):")
only_apple = vector_search(question, ticker="AAPL")
print("  rows found:", len(only_apple))
for r in only_apple:
    print("  ", r)

print("\nSearch everything:")
everything = vector_search(question)
for r in everything:
    print("  ", r)

print("\nBefore and after embedding Apple")
print(f"  best similarity   {before['best']:.3f}  ->  {max(float(r[3]) for r in everything):.3f}")
print(f"  companies found   {before['companies']}  ->  {sorted({r[0] for r in everything})}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

with pg() as cx:
    total = cx.execute(f"SELECT count(*) FROM {SCHEMA}.chunks").fetchone()[0]
    with_vectors = cx.execute(f"SELECT count(*) FROM {SCHEMA}.chunks WHERE embedding IS NOT NULL").fetchone()[0]

check(total == len(rows), f"all {len(rows)} chunks are in the table")
check(with_vectors == total, "every chunk has a vector")
check(len(only_apple) == 5, "the Apple-only search now returns results")
check({r[0] for r in everything} == {"AAPL"}, "searching everything now returns only Apple chunks for an Apple question")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What to notice
# MAGIC
# MAGIC - The question, the data and the model are identical before and after. Only the index changed.
# MAGIC - One database holds the text, the metadata and the vectors, so a filter and a similarity search happen in a single SQL query.
# MAGIC
# MAGIC **Your turn:** run `vector_search("How does Walmart describe competition?")` and read the similarity scores. Then try a question that has nothing to do with the filings.
# MAGIC
# MAGIC **Next:** `04_search`
