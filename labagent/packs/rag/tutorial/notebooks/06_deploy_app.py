# Databricks notebook source
# MAGIC %md
# MAGIC # 06. Deploy the assistant as a Databricks App
# MAGIC
# MAGIC **Time:** about 10 minutes. This notebook is optional: notebooks 00 to 05 already built a working RAG system.
# MAGIC
# MAGIC A **Databricks App** is a small web service that Databricks hosts for you, behind your workspace login. The app code (a FastAPI service and a web page) was uploaded next to these notebooks in the `app` folder. You will not write it. You will do the four things a deployment script would automate:
# MAGIC
# MAGIC 1. Create the app and tell it which models it may call.
# MAGIC 2. Give the app's own identity a **read-only** database role. The app never uses your credentials.
# MAGIC 3. Fill in the app's settings (database host, model names).
# MAGIC 4. Deploy, and wait until it runs.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]>=3.1" --quiet

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. Before you start
# MAGIC
# MAGIC The data must be loaded, and the app code must be where labagent uploaded it.

# COMMAND ----------

import os
import shutil

with pg() as cx:
    chunks = cx.execute(f"SELECT count(*) FROM {SCHEMA}.chunks").fetchone()[0]
print("chunks in the database:", chunks)
print("app code folder:", f"{TUTORIAL_FOLDER}/app", "| exists:", os.path.isdir(f"{TUTORIAL_FOLDER}/app"))
check(chunks > 0, "the database has data (run notebook 03 if this fails)")
check(os.path.isdir(f"{TUTORIAL_FOLDER}/app"), "the app code was uploaded (launch the tutorial with labagent, or upload the app folder yourself)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Create the app
# MAGIC
# MAGIC The app is given **resources**: permission to query exactly two serving endpoints, the embedding model and the chat model. Nothing else.
# MAGIC
# MAGIC Free Edition allows three apps and stops each one after 24 hours. You can start it again later.

# COMMAND ----------

from databricks.sdk.errors import NotFound

def get_app():
    try:
        return w.api_client.do("GET", f"/api/2.0/apps/{APP_NAME}")
    except NotFound:
        return None

if get_app() is None:
    w.api_client.do("POST", "/api/2.0/apps", body={
        "name": APP_NAME, "description": "RAG over SEC 10-K filings (labagent tutorial)",
        "resources": [{"name": "embedder", "serving_endpoint": {"name": EMBED_ENDPOINT, "permission": "CAN_QUERY"}},
                      {"name": "chat", "serving_endpoint": {"name": CHAT_ENDPOINT, "permission": "CAN_QUERY"}}]})
    print("app created")
else:
    print("app already exists")

service_principal = None
for _ in range(30):
    service_principal = _find(get_app(), "service_principal_client_id")
    if service_principal:
        break
    time.sleep(5)
print("the app's own identity (service principal):", service_principal)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Give the app a read-only database role
# MAGIC
# MAGIC Two steps. Create a Postgres role for the app's identity (it signs in with an OAuth token, no password). Then grant it `SELECT` on the one table it needs. If the app is ever compromised it can read the filings and nothing else.

# COMMAND ----------

role_id = re.sub(r"[^a-z0-9-]", "-", f"app-{APP_NAME}".lower())
try:
    w.api_client.do("POST", f"/api/2.0/postgres/{BRANCH}/roles", query={"role_id": role_id},
                    body={"spec": {"postgres_role": service_principal, "identity_type": "SERVICE_PRINCIPAL", "auth_method": "LAKEBASE_OAUTH_V1"}})
    print("role created")
except Exception as e:
    if "already exists" not in str(e).lower():
        raise
    print("role already exists")

with pg() as cx:
    cx.execute(f'GRANT USAGE ON SCHEMA {SCHEMA} TO "{service_principal}"')
    cx.execute(f'GRANT SELECT ON {SCHEMA}.chunks TO "{service_principal}"')
print("granted: USAGE on the schema and SELECT on the chunks table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Fill in the app's settings
# MAGIC
# MAGIC `app.yaml` tells Databricks how to start the app and which environment variables to set. The uploaded copy has placeholders. You copy the folder and replace them with the values you created in this tutorial.

# COMMAND ----------

source, target = f"{TUTORIAL_FOLDER}/app", f"{TUTORIAL_FOLDER}/app_deploy"
if os.path.isdir(target):
    shutil.rmtree(target)
shutil.copytree(source, target)

settings = {"PGHOST": lakebase_host(), "LAKEBASE_ENDPOINT": LAKEBASE_ENDPOINT, "EMBED_ENDPOINT": EMBED_ENDPOINT,
            "CHAT_ENDPOINT": CHAT_ENDPOINT, "RAG_SCHEMA": SCHEMA, "TOP_K": str(TOP_K)}
yaml_path = f"{target}/app.yaml"
text = open(yaml_path, encoding="utf-8").read()
for key, value in settings.items():
    text = text.replace(f"__{key}__", value)
open(yaml_path, "w", encoding="utf-8").write(text)

print(text)
check("__" not in text, "every placeholder in app.yaml was replaced")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Deploy
# MAGIC
# MAGIC A deployment snapshots the folder and starts it. A brand-new app first needs its compute to start, which takes a few minutes, so the next cell waits for it.

# COMMAND ----------

def compute_state():
    return (get_app().get("compute_status") or {}).get("state")

if compute_state() in ("STOPPED", "ERROR"):
    w.api_client.do("POST", f"/api/2.0/apps/{APP_NAME}/start")
    print("the app's compute was stopped: starting it ...")

# A new app's compute needs a few minutes to start, and Databricks refuses a deployment until it is ACTIVE.
for _ in range(60):
    if compute_state() == "ACTIVE":
        break
    print("  app compute:", compute_state())
    time.sleep(10)
check(compute_state() == "ACTIVE", "the app's compute is active, so it can be deployed")

deployment = w.api_client.do("POST", f"/api/2.0/apps/{APP_NAME}/deployments", body={"source_code_path": target, "mode": "SNAPSHOT"})
print("deployment id:", deployment.get("deployment_id"))

# COMMAND ----------

state, url = None, None
for _ in range(60):
    app = get_app()
    state = (app.get("app_status") or {}).get("state")
    dep = (app.get("active_deployment") or app.get("pending_deployment") or {}).get("status", {}).get("state")
    url = app.get("url")
    print(f"  app: {state} | deployment: {dep}")
    if state == "RUNNING" and dep == "SUCCEEDED":
        break
    if dep == "FAILED":
        raise RuntimeError("the deployment failed: open the app in Compute > Apps and read its logs")
    time.sleep(10)
print("\napp url:", url)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

check(state == "RUNNING", "the app is running")
check(url and url.startswith("https://"), "the app has a URL")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Try it
# MAGIC
# MAGIC Open the URL above in a new browser tab and sign in with your Databricks account. Pick a suggested question, or ask your own. Try the company filter. Open one of the source links: it goes to the original filing on sec.gov.
# MAGIC
# MAGIC ## What to notice
# MAGIC
# MAGIC - The app uses the **same** search and the **same** prompt you wrote in notebooks 04 and 05, served behind a web page.
# MAGIC - It runs with its **own identity** and a read-only role, not with yours.
# MAGIC - On Free Edition the app stops after 24 hours. Start it again from **Compute > Apps**.
# MAGIC
# MAGIC **Next:** `99_cleanup` when you are finished.
