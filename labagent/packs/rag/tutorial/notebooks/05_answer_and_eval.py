# Databricks notebook source
# MAGIC %md
# MAGIC # 05. Answer with citations, then measure
# MAGIC
# MAGIC **Time:** about 15 minutes.
# MAGIC
# MAGIC Retrieval finds passages. **Generation** gives them to a chat model and asks it to answer using only those passages and to cite them. Then you **evaluate**: write questions with known answers and measure how often retrieval finds the right place. Without a score you cannot tell whether a change helped.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]>=3.1" --quiet

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ./_common

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. The answer function
# MAGIC
# MAGIC Four steps: embed the question, retrieve passages, number them, ask the chat model. The instruction in `SYSTEM` is what stops the model from answering from memory.

# COMMAND ----------

SYSTEM = ("You answer questions using only the numbered context passages. Cite the passages you used like [1] or [2]. "
          "If the context does not contain the answer, say you do not know. Do not use outside knowledge.")


def ask(question, filters=None, k=TOP_K):
    qvec = embed([question])[0]
    with pg() as cx:
        hits = hybrid_search(cx, qvec, question, k, filters)
    if not hits:
        return {"answer": "I do not know: no matching passages.", "sources": []}
    context = "\n\n".join(f"[{i}] {h['title']}\n{h['content']}" for i, h in enumerate(hits, 1))
    out = w.api_client.do("POST", f"/serving-endpoints/{CHAT_ENDPOINT}/invocations", body={
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}],
        "max_tokens": 1200})        # reasoning models spend tokens thinking first: 500 cut answers off in testing
    choice = out["choices"][0]
    return {"answer": choice["message"]["content"], "sources": hits, "cut_off": choice.get("finish_reason") == "length"}


def show_answer(result):
    print(result["answer"])
    if result.get("cut_off"):
        print("\n[the model ran out of tokens: raise max_tokens in ask()]")
    print("\ncites passage numbers like [1]:", bool(re.search(r"\[\d+\]", result["answer"])))
    print("\nsources:")
    for i, h in enumerate(result["sources"], 1):
        print(f"  [{i}] {h['title']}  ->  {h['meta'].get('url')}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Ask a question the filings can answer

# COMMAND ----------

good = ask("What are Apple's main supply chain and manufacturing risks?", filters={"ticker": "AAPL", "fiscal_year": "2025"})
show_answer(good)

# COMMAND ----------

# MAGIC %md
# MAGIC Each number like `[1]` maps to a passage and a link to the original filing, so a reader can check the answer.
# MAGIC
# MAGIC **Models do not always cite.** In testing, about 1 answer in 9 left out the numbers even though the instruction asked for them. Run the cell a few times. A production system checks for citations and retries or flags the answer.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Ask a question the filings cannot answer
# MAGIC
# MAGIC A good system says it does not know. This is the behaviour to test before you trust a RAG system.

# COMMAND ----------

bad = ask("What is the recipe for Walmart's store-brand cookies?")
show_answer(bad)

# COMMAND ----------

# MAGIC %md
# MAGIC Models differ here. If yours declines, that is the good outcome. If it answers anyway, look at the sources: they are real passages about something else, and the model stretched them.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Evaluate retrieval
# MAGIC
# MAGIC Each case is a question and **where the answer should come from**: a company and a section (`1` Business, `1A` Risk Factors, `7` Management's Discussion). A case **hits** when any retrieved passage comes from that company and section.

# COMMAND ----------

cases = [
    ("What are Apple's main supply chain and manufacturing risks?", {"ticker": "AAPL", "item": "1A"}),
    ("How does Apple describe its products and services?", {"ticker": "AAPL", "item": "1"}),
    ("What credit and interest rate risks does JPMorgan Chase face?", {"ticker": "JPM", "item": "1A"}),
    ("How did JPMorgan Chase's net income and revenue change this year?", {"ticker": "JPM", "item": "7"}),
    ("What patent expirations and generic competition risks does Pfizer disclose?", {"ticker": "PFE", "item": "1A"}),
    ("What are Pfizer's main product lines and business segments?", {"ticker": "PFE", "item": "1"}),
    ("How do oil and gas price changes affect Chevron's earnings?", {"ticker": "CVX", "item": "7"}),
    ("What climate and regulatory risks does Chevron identify?", {"ticker": "CVX", "item": "1A"}),
    ("How did Walmart's comparable sales and operating income perform?", {"ticker": "WMT", "item": "7"}),
    ("What competition and supply risks does Walmart face?", {"ticker": "WMT", "item": "1A"}),
]

rows = load_rows()
in_data = lambda expect: any(all(str(r["meta"].get(k)) == str(v) for k, v in expect.items()) for r in rows)
applicable = [c for c in cases if in_data(c[1])]
missing = [c for c in cases if not in_data(c[1])]
print(f"{len(cases)} questions; {len(missing)} ask for a section that is not in the dataset:")
for q, expect in missing:
    print("   not in the data:", expect, "-", q[:60])

# COMMAND ----------

qvecs = embed([q for q, _ in applicable])
hits = 0
with pg() as cx:
    for (q, expect), qv in zip(applicable, qvecs):
        results = hybrid_search(cx, qv, q, k=TOP_K)
        ok = is_hit(results, expect)
        hits += ok
        print(("HIT  " if ok else "MISS ") + str(expect).ljust(36), q[:70])
rate = hits / len(applicable)
print(f"\nretrieval hit rate: {rate:.2f} on {len(applicable)} questions that the data can answer")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. The quality gate
# MAGIC
# MAGIC A deployment can refuse to publish an app whose hit rate is below **0.6**. That rule is the point: a gate turns "it seems to work" into a number you can enforce.

# COMMAND ----------

GATE = 0.6
print(f"hit rate {rate:.2f} vs gate {GATE}: {'PASS, safe to deploy' if rate >= GATE else 'FAIL, do not deploy'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check

# COMMAND ----------

check(len(good["sources"]) > 0 and not good["cut_off"], "the answer was complete and came with its sources")
check(all(h["meta"].get("url", "").startswith("https://www.sec.gov/") for h in good["sources"]), "every source links to the original filing on sec.gov")
check(rate >= GATE, f"retrieval hit rate {rate:.2f} is at or above the {GATE} gate")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What to notice
# MAGIC
# MAGIC - The two questions about JPMorgan and Chevron *Management's Discussion* were set aside because **the data does not contain those sections**. No retrieval setting can find what is not there. Evaluation tells you whether to fix **retrieval** or the **data**.
# MAGIC - Change `TOP_K` or the chunking and re-run section 4. Did the rate go up or down? That is how you improve a RAG system: change one thing, measure.
# MAGIC
# MAGIC **Next:** `06_deploy_app`
