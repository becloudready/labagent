# Databricks notebook source
# MAGIC %md
# MAGIC # 00. Set up: the pieces and the database
# MAGIC
# MAGIC **Time:** about 5 minutes. **Cost:** none on Databricks Free Edition.
# MAGIC
# MAGIC You will build a research assistant that answers questions from real SEC annual reports (Form 10-K) and cites its sources. This tutorial builds it one step at a time, so you see what each part does and why it is there.
# MAGIC
# MAGIC ```
# MAGIC  question --> [embedding model] --> vector --> [Lakebase: Postgres + pgvector] --> best passages
# MAGIC                                                                                      |
# MAGIC  answer with citations <-- [chat model] <---------------- question + passages <------+
# MAGIC ```
# MAGIC
# MAGIC | Notebook | You will |
# MAGIC |---|---|
# MAGIC | 00 Set up | Check your workspace and create the database |
# MAGIC | 01 Data | Load and explore the SEC filings |
# MAGIC | 02 Embeddings | Turn text into vectors |
# MAGIC | 03 pgvector | Store vectors in Lakebase and search them |
# MAGIC | 04 Search | Add indexes, keyword search, hybrid search and filters |
# MAGIC | 05 Answer and evaluate | Generate cited answers and score retrieval |
# MAGIC | 06 Deploy the app | Publish the assistant as a Databricks App |
# MAGIC | 99 Clean up | Remove everything you created |
# MAGIC
# MAGIC Run the notebooks in order. Run each cell with **Shift+Enter**. Every notebook ends with a check cell that says whether the step worked.

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Check your workspace
# MAGIC
# MAGIC Two kinds of model are needed. An **embedding model** turns text into numbers. A **chat model** writes the answer. Databricks hosts both as pay-per-token endpoints.

# COMMAND ----------

print("signed in as:      ", ME)
print("embedding endpoint:", EMBED_ENDPOINT)
print("chat endpoint:     ", CHAT_ENDPOINT)
check(EMBED_ENDPOINT and CHAT_ENDPOINT, "an embedding model and a chat model are available in this workspace")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create the database
# MAGIC
# MAGIC **Lakebase** is managed Postgres inside Databricks. The **pgvector** extension adds a vector column type and similarity search, so one database can hold your text, its metadata and its vectors.
# MAGIC
# MAGIC Free Edition allows **one** Lakebase project. The cell below creates a project called `rag-tutorial`. It does nothing if the project already exists, and it stops with a message if you already have a different project.

# COMMAND ----------

projects = [p["name"] for p in w.api_client.do("GET", "/api/2.0/postgres/projects").get("projects", [])]
print("existing projects:", projects)

if f"projects/{PROJECT}" in projects:
    print(f"project '{PROJECT}' already exists: nothing to create")
elif projects:
    raise RuntimeError(f"this workspace already has the Lakebase project {projects[0]!r} and Free Edition allows one. "
                       f"Delete it in the Lakebase page, or set PROJECT in the _common notebook to its name.")
else:
    w.api_client.do("POST", "/api/2.0/postgres/projects", query={"project_id": PROJECT}, body={})
    print(f"creating project '{PROJECT}' ...")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Wait for the database to start
# MAGIC
# MAGIC A new project takes a minute or two to get its compute endpoint.

# COMMAND ----------

host = None
for _ in range(40):
    try:
        host = lakebase_host()
    except Exception:
        host = None
    if host:
        break
    print("  still starting ...")
    time.sleep(8)
print("database host:", host)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

check(host, "the Lakebase endpoint has a hostname")
token = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": LAKEBASE_ENDPOINT}).get("token")
check(token, "you can get a short-lived database credential")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What to notice
# MAGIC
# MAGIC - There is **no password to manage**. You ask Databricks for a token that lasts about an hour, and use your own identity as the database user.
# MAGIC - The database is empty. Everything in the next notebooks creates something inside it, and notebook 99 removes it.
# MAGIC
# MAGIC **Next:** `01_data`
