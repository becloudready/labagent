# Databricks notebook source
# MAGIC %md
# MAGIC # 01. The data: what are we searching?
# MAGIC
# MAGIC **Time:** about 5 minutes.
# MAGIC
# MAGIC RAG is only as good as the passages it can find. Before any embedding, look at the data.
# MAGIC
# MAGIC The filings were already split into **chunks**, short passages that stay inside one section of a 10-K. Each chunk carries **metadata**: company, year, section and the URL of the original on sec.gov. You will use the metadata later for filters and citations.

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Download the dataset
# MAGIC
# MAGIC The download is a versioned release on GitHub. The code checks its SHA-256 against a pinned value, so you and every other student work on identical data.

# COMMAND ----------

rows = load_rows()
print(len(rows), "chunks loaded")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Look at one chunk

# COMMAND ----------

one = rows[0]
print("id:     ", one["id"])
print("title:  ", one["title"])
print("meta:   ", json.dumps(one["meta"], indent=2))
print("content:", one["content"][:500], "...")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. What is in the dataset?

# COMMAND ----------

from collections import Counter

print("by company:   ", dict(Counter(r["meta"]["ticker"] for r in rows)))
print("by section:   ", dict(Counter(r["meta"]["section"] for r in rows)))
print("by year:      ", sorted(Counter((r["meta"]["ticker"], r["meta"]["fiscal_year"]) for r in rows).items()))
lengths = sorted(len(r["content"]) for r in rows)
print(f"chunk length:  shortest {lengths[0]}, median {lengths[len(lengths) // 2]}, longest {lengths[-1]} characters")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

check(len(rows) == 1880, "all 1,880 chunks loaded")
check({r["meta"]["ticker"] for r in rows} == {"AAPL", "CVX", "JPM", "PFE", "WMT"}, "five companies: Apple, Chevron, JPMorgan, Pfizer, Walmart")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What to notice
# MAGIC
# MAGIC - Chunks are **about 1,000 characters**. Too small and a passage loses its context. Too large and one vector blurs several topics.
# MAGIC - Each company has **two fiscal years**, so the same question can match two near-identical passages.
# MAGIC - **Known gap:** the JPMorgan and Chevron *Management's Discussion and Analysis* sections are missing from this dataset. You will see this show up in the evaluation in notebook 05.
# MAGIC
# MAGIC **Your turn:** find the longest chunk and read it. Does it look like a good unit to search?
# MAGIC
# MAGIC **Next:** `02_embeddings`
