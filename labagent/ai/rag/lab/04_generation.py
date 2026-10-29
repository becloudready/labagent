# %% [markdown]
# # 4. Generation: make the model answer from what you found
#
# **Time:** about 25 minutes. **Cost:** nothing with the mock or Ollama; fractions of a cent with a Groq key. **Needs:** notebooks 1 to 3.
#
# The "G" in RAG. The language model has never seen your documents. The only way it learns what they say is the **prompt** you
# send. So the quality of the answer depends on three things you control: which chunks you retrieved, how you present them,
# and what you tell the model to do with them.
#
# ## Which model answers?
#
# - **No key:** a clearly marked *mock* answer, so every cell still runs. Fine for building the prompt, which is the exercise.
# - **Free Groq key (recommended):** create one at console.groq.com, put it in `.env` as `GROQ_API_KEY=...`, restart the kernel.
# - **Ollama:** run `ollama serve` and pull a model, then set `LLM_PROVIDER=ollama` in `.env`.
#
# The function `chat(system, user)` in `labagent/ai/rag/labkit.py` sends one request and tells you which provider answered.

# %%
import numpy as np
from sentence_transformers import SentenceTransformer
from labagent.ai.rag.labkit import chat, check, load_docs, load_env

load_env()
docs = load_docs()
model = SentenceTransformer("all-MiniLM-L6-v2")

# %% [markdown]
# ## Starting point
#
# Working versions of the chunker and the store from notebooks 1 and 3. Paste yours over them if you like.

# %%
#### REFERENCE chunk_fixed
#### REFERENCE chunk_paragraphs
#### REFERENCE TinyStore

chunks = [{"id": f"{name}#{i}", "doc": name, "text": text}
          for name, doc_text in docs.items()
          for i, text in enumerate(chunk_paragraphs(doc_text, 500, 50))]
store = TinyStore()
store.add(chunks)
print(len(store), "chunks ready")

# %% [markdown]
# ## Exercise 1: write the prompt
#
# `build_prompt(question, hits)` returns two strings, `(system, user)`.
#
# **system** is the standing instruction. It must tell the model to
# 1. answer **only** from the numbered sources,
# 2. say it does not know when the sources do not contain the answer (do not let it guess), and
# 3. cite the source numbers it used, like `[1]`.
#
# **user** holds the evidence and the question. Number each hit starting at 1, show which document it came from, then the
# question last. Models pay more attention to the end of a long prompt.
#
# A layout that works:
# ```
# [1] (security_policy)
# <chunk text>
#
# [2] (data_handling)
# <chunk text>
#
# Question: <the question>
# ```

# %%
def build_prompt(question, hits):
    """Return (system, user)."""
    #### SOLUTION build_prompt
    system = ("You answer questions about company policy. Use ONLY the numbered sources in the user message. "
              "If the sources do not contain the answer, reply exactly: I don't know based on the documents. "
              "Cite the sources you used like [1] or [2]. Keep the answer short.")
    sources = "\n\n".join(f"[{i}] ({h['doc']})\n{h['text']}" for i, h in enumerate(hits, 1))
    return system, f"{sources}\n\nQuestion: {question}"
    #### STUDENT
    # TODO: a system message with the three rules above
    # TODO: a user message: the numbered sources, then the question
    return "", ""
    #### END

# %%
hits = store.search("How long do we keep customer records?", k=3)
system, user = build_prompt("How long do we keep customer records?", hits)
check("the system message tells the model to use only the sources", "only" in system.lower(), "say ONLY the sources")
check("the system message says what to do when there is no answer", "know" in system.lower(), "tell it to say it does not know")
check("the system message asks for citations", "cite" in system.lower() or "[1]" in system, "ask for [1]-style citations")
check("every hit is in the user message, numbered", all(f"[{i}]" in user and h["text"] in user for i, h in enumerate(hits, 1)), "number the sources from 1 and include the full text")
check("the source document names are shown", all(h["doc"] in user for h in hits), "put (doc name) after each number")
check("the question comes last", user.rstrip().endswith("How long do we keep customer records?"), "end the user message with the question")

# %% [markdown]
# ## What the model really sees
#
# Print the prompt. This is everything the model knows: nothing else about your company reaches it.

# %%
print("SYSTEM:", system, "\n")
print("USER:\n" + user)

# %% [markdown]
# ## Exercise 2: the whole pipeline in one function
#
# Connect retrieval and generation. `answer(question, k)` retrieves `k` chunks, builds the prompt, calls `chat`, and returns
# a dict with the reply and the hits (so you can show your sources).

# %%
def answer(question, k=3):
    """Return {"answer": str, "provider": str, "hits": [...], "prompt_tokens": int}."""
    #### SOLUTION answer
    hits = store.search(question, k=k)
    system, user = build_prompt(question, hits)
    reply = chat(system, user)
    return {"answer": reply.text, "provider": reply.provider, "hits": hits, "prompt_tokens": reply.prompt_tokens}
    #### STUDENT
    # TODO: search, build the prompt, call chat(system, user), return the dict described above
    return {}
    #### END

# %%
r = answer("How often must secrets be rotated?")
check("it returns the answer, the provider and the hits", {"answer", "provider", "hits"} <= set(r), "return a dict with those keys")
check("it used k=3 hits by default", len(r["hits"]) == 3, "pass k to the store search")
print("provider:", r["provider"], "| prompt tokens:", r["prompt_tokens"])
print(r["answer"])
print("sources:", [h["id"] for h in r["hits"]])

# %% [markdown]
# With the mock provider the answer is a placeholder, and the prompt above is the real result of this exercise. With a Groq
# or Ollama model you should get a short answer ending in a citation such as `[1]`.
#
# ## Experiment: what happens when the answer is not in the documents
#
# Run the two questions. A real model told to answer only from sources should reply "I don't know". If the model answers the
# second one anyway, it used its own memory instead of your documents. That is a failure you must test for, not assume away.

# %%
for q in ["What is the password length rule?", "Who is the CEO of Acme Corp?"]:
    r = answer(q)
    print(f"Q: {q}\nA ({r['provider']}): {r['answer']}\n")

# %% [markdown]
# ## What it costs
#
# Every retrieved chunk is paid for on every question. Prompt size grows with `k` and with chunk size, and so does the bill
# with a paid model. Run it and use your own provider's price list in the last line.

# %%
for k in (1, 3, 5, 10):
    system, user = build_prompt("How often must secrets be rotated?", store.search("How often must secrets be rotated?", k=k))
    print(f"k={k:>2}: prompt about {(len(system) + len(user)) // 4:>5} tokens (characters / 4, a rough estimate)")
PRICE_PER_MILLION_INPUT_TOKENS = 0.50     # dollars. An example number: replace it with your provider's current price
tokens = (len(system) + len(user)) // 4
print(f"\n1,000 questions at k=10 would send about {tokens * 1000:,} input tokens: about ${tokens * 1000 / 1e6 * PRICE_PER_MILLION_INPUT_TOKENS:.2f} at the example price")

# %% [markdown]
# **Your turn (write your answer):** you can lower the cost by lowering `k` or by shrinking chunks. What does each choice risk
# in terms of the answer being found? (You will measure this in notebook 7.)
#
# *Your answer:*

# %% [markdown]
# ## What to take away
#
# - RAG is retrieval plus a prompt. There is no other channel from your documents to the model.
# - "Answer only from the sources, and say when you cannot" is the single most useful instruction, and it must be tested.
# - Show the sources next to the answer, so a person can check.
# - Cost and latency scale with how much text you retrieve.
#
# **Next:** notebook 5 swaps your `TinyStore` for a real vector database and adds metadata filters.
