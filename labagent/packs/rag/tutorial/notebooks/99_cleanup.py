# Databricks notebook source
# MAGIC %md
# MAGIC # 99. Clean up
# MAGIC
# MAGIC Run this when you have finished the tutorial. It removes what you created, in the reverse order. Each step is safe to run twice.
# MAGIC
# MAGIC Free Edition allows **one** Lakebase project and **three** apps, so cleaning up also frees those slots for your next project.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]>=3.1" --quiet

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Delete the app

# COMMAND ----------

from databricks.sdk.errors import NotFound

try:
    w.api_client.do("DELETE", f"/api/2.0/apps/{APP_NAME}")
    print("app deleted (it may show as DELETING for a minute)")
except NotFound:
    print("no app to delete")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Drop the tutorial schema
# MAGIC
# MAGIC This deletes the table, the vectors and the indexes. It leaves the database project itself.

# COMMAND ----------

try:
    with pg() as cx:
        cx.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    print(f"schema '{SCHEMA}' dropped")
except Exception as e:
    print("nothing to drop:", str(e)[:120])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Delete the database project
# MAGIC
# MAGIC **This cannot be undone.** Lakebase keeps a deleted project for a while, which blocks its name. The second call purges it so you can reuse the name.
# MAGIC
# MAGIC Run the next cell only if you want to remove the whole database.

# COMMAND ----------

DELETE_PROJECT = True      # set to False to keep the database project

if DELETE_PROJECT:
    for suffix in ("", "?purge=true"):
        try:
            w.api_client.do("DELETE", f"/api/2.0/postgres/projects/{PROJECT}{suffix}")
        except NotFound:
            pass
        except Exception as e:
            print("delete request:", str(e)[:140])
    time.sleep(5)
    remaining = [p["name"] for p in w.api_client.do("GET", "/api/2.0/postgres/projects").get("projects", [])]
    print("projects left:", remaining)
else:
    print("kept the project")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

app_gone = False
for _ in range(24):                      # deletion is asynchronous: give it up to two minutes
    try:
        app = w.api_client.do("GET", f"/api/2.0/apps/{APP_NAME}")
    except NotFound:
        app_gone = True
        break
    if "DELETING" in ((app.get("app_status") or {}).get("state"), (app.get("compute_status") or {}).get("state")):
        app_gone = True
        break
    time.sleep(5)
check(app_gone, "the app is deleted or being deleted")
if DELETE_PROJECT:
    check(f"projects/{PROJECT}" not in remaining, "the Lakebase project is gone")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Last step
# MAGIC
# MAGIC The notebooks themselves are still in your workspace folder. Delete the folder from the workspace sidebar, or from your terminal:
# MAGIC
# MAGIC ```
# MAGIC labagent destroy rag-sec-tutorial --name <the name you used>
# MAGIC ```
