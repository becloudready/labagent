# %% [markdown]
# # AML lab 3: citations you can trust
#
# **Time:** about 30 minutes. **Cost:** nothing (a Groq key makes the draft real). **Needs:** AML labs 1 and 2.
#
# An AML investigator writes a **narrative** for a regulator: who, what, when, where, why, how. Every claim in it must trace to a
# transaction or a rule. A language model drafts fluent text, and fluent text can cite a transaction that does not exist or a section
# that was never written. In this lab you build the **guard** that catches that, and the **prompt** that asks for a narrative in the first place.
#
# ## Starting point
#
# Working code from labs 1 and 2: the corpus, your `search`, your `bundle`, and the tables.

# %%
#### REFERENCE corpus_setup
#### REFERENCE store_setup
#### REFERENCE search
#### REFERENCE bundle_setup
#### REFERENCE helpers
#### REFERENCE transactions_for
#### REFERENCE truth_setup
#### REFERENCE similar_cases
#### REFERENCE bundle
#### REFERENCE render_setup

import re
from labagent.ai.rag.labkit import chat
escalate_ids = [a["id"] for a in alerts.values() if truth[a["id"]]["label"] == "escalate"]
b = bundle(escalate_ids[0])
print(f"bundle for {b['alert']['id']}: {len(b['transactions'])} transfers, {len(b['policy'])} policy chunks, {len(b['regulations'])} regulation chunks")

# %% [markdown]
# ## What can be checked, and what cannot
#
# Three kinds of reference can be checked mechanically against the data:
#
# | Reference | Looks like | It is valid if |
# |---|---|---|
# | transaction | `T0004315` (a `T` and 7 digits) | it is in the transactions table |
# | regulation | `PCMLTFA s. 7`, `PCMLTF Regulations s. 10`, `31 CFR 1020.320` | it is the `citation` of a regulation chunk |
# | policy | `policy section 8` | 8 is a section of the policy manual |
#
# A fourth check is **support**: a transaction ID can exist and still belong to a different customer. It is only valid for *this*
# alert if it is in this alert's bundle.
#
# Here is the data you will check against:

# %%
known_tx = {t["id"] for t in transactions}
known_regs = {c["citation"] for c in chunks if c.get("doc_type") == "regulation"}
known_sections = {c["section"] for c in chunks if c.get("doc_type") == "policy"}
print(len(known_tx), "transactions;", len(known_regs), "regulation citations, e.g.", sorted(known_regs)[:3], ";", len(known_sections), "policy sections:", sorted(known_sections, key=int))

# %% [markdown]
# ## Exercise 1: the citation checker
#
# `check_citations(text, bundle=None)` finds every reference of the three kinds in `text` and returns a dict:
#
# - `cited`: how many distinct references it found,
# - `invalid`: the references that do not exist (a transaction not in the table, a citation not in `known_regs`, a policy section not in `known_sections`),
# - `unsupported`: if a `bundle` is given, the transaction IDs that exist but are not in `bundle["transactions"]`,
# - `rate`: `(cited - len(invalid) - len(unsupported)) / cited`, or `1.0` when nothing is cited,
# - `by_kind`: `{"transactions": n, "regulations": n, "policy": n}` of what was found.
#
# Patterns to use: transactions `\bT\d{7}\b`; regulations `(?:PCMLTFA|PCMLTF Regulations|PCMLTF STR Regulations) s\. \d+(?:\.\d+)?|31 CFR \d+\.\d+`;
# policy `policy section (\d+)` (ignore case). Count each distinct reference once.

# %%
TX_RE = re.compile(r"\bT\d{7}\b")
REG_RE = re.compile(r"(?:PCMLTFA|PCMLTF Regulations|PCMLTF STR Regulations) s\. \d+(?:\.\d+)?|31 CFR \d+\.\d+")
POLICY_RE = re.compile(r"policy section (\d+)", re.I)

def check_citations(text, bundle=None):
    """Which references in `text` are invalid, and which are unsupported by this alert's evidence."""
    #### SOLUTION check_citations
    tx = sorted(set(TX_RE.findall(text)))
    regs = sorted(set(REG_RE.findall(text)))
    policy = sorted(set(POLICY_RE.findall(text)))
    invalid = [t for t in tx if t not in known_tx] + [r for r in regs if r not in known_regs] + [f"policy section {p}" for p in policy if p not in known_sections]
    unsupported = []
    if bundle is not None:
        in_bundle = {t["id"] for t in bundle["transactions"]}
        unsupported = [t for t in tx if t in known_tx and t not in in_bundle]
    cited = len(tx) + len(regs) + len(policy)
    rate = 1.0 if cited == 0 else (cited - len(invalid) - len(unsupported)) / cited
    return {"cited": cited, "invalid": invalid, "unsupported": unsupported, "rate": round(rate, 3),
            "by_kind": {"transactions": len(tx), "regulations": len(regs), "policy": len(policy)}}
    #### STUDENT
    # TODO: find the three kinds of reference with the patterns above (use set() so each counts once)
    # TODO: invalid = does not exist; unsupported = exists but not in this alert's bundle
    # TODO: rate and by_kind as described
    return {"cited": 0, "invalid": [], "unsupported": [], "rate": 1.0, "by_kind": {"transactions": 0, "regulations": 0, "policy": 0}}
    #### END

# %%
real = [t["id"] for t in b["transactions"][:2]]
reg = sorted(known_regs)[0] if known_regs else None            # a lab launched with regulations=skip has none
elsewhere = next(t["id"] for t in transactions if t["id"] not in {x["id"] for x in b["transactions"]})
good = f"Transfers {real[0]} and {real[1]} match policy section 8." + (f" See {reg}." if reg else "")
ok = check_citations(good, b)
check("a fully valid text scores 1.0", ok["rate"] == 1.0 and ok["cited"] == (4 if reg else 3), f"got {ok}")
check("it counts the kinds", ok["by_kind"] == {"transactions": 2, "regulations": 1 if reg else 0, "policy": 1}, f"got {ok['by_kind']}")
check("an invented transaction is invalid", check_citations("See T9999999.")["invalid"] == ["T9999999"], "T9999999 is not in the table")
check("an invented regulation section is invalid", check_citations("See PCMLTFA s. 999.")["invalid"] == ["PCMLTFA s. 999"], "compare with known_regs (nothing is known when the lab has no regulations, so every citation is invalid)")
check("an invented policy section is invalid", check_citations("Under policy section 99 this applies.")["invalid"] == ["policy section 99"], "compare with known_sections")
mix = check_citations(f"{real[0]}, {elsewhere}, T9999999", b)
check("a real transaction from another alert is unsupported, not invalid", mix["unsupported"] == [elsewhere] and mix["invalid"] == ["T9999999"], f"got {mix}")
check("a repeated reference counts once", check_citations(f"{real[0]} {real[0]} {real[0]}")["cited"] == 1, "use set()")
check("no citations means rate 1.0", check_citations("Nothing cited here.")["rate"] == 1.0 and check_citations("Nothing cited here.")["cited"] == 0, "avoid dividing by zero")

# %% [markdown]
# **Your turn (write your answer):** the last check passes a text that cites nothing at all with a rate of 1.0. Why is that a trap if
# you use `rate == 1.0` as a quality gate on model output? What other condition would you add?
#
# *Your answer:*
#
# ## Exercise 2: a gate that cannot be passed by saying nothing
#
# Write `acceptable(result)`: true only if `rate == 1.0` **and** the text cites at least 2 transactions **and** at least one rule
# (a regulation citation or a policy section).

# %%
def acceptable(result):
    #### SOLUTION acceptable
    k = result["by_kind"]
    return result["rate"] == 1.0 and k["transactions"] >= 2 and (k["regulations"] + k["policy"]) >= 1
    #### STUDENT
    # TODO: rate is 1.0, at least 2 transactions, and at least one regulation or policy section
    return False
    #### END

# %%
check("a well-cited narrative is acceptable", acceptable(check_citations(good, b)), "needs rate 1.0, 2 transactions and a rule")
check("a text with no citations is not", not acceptable(check_citations("Suspicious activity occurred.", b)), "saying nothing must fail")
check("an invented citation is not", not acceptable(check_citations(f"{real[0]} {real[1]} T9999999 policy section 8", b)), "any invalid reference fails the gate")
check("citing only transactions is not enough", not acceptable(check_citations(f"{real[0]} {real[1]}", b)), "a rule must be cited as well")

# %% [markdown]
# ## Exercise 3: ask for a narrative
#
# Now the request itself. `narrative_messages(b)` returns `(system, user)`.
#
# **system** must tell the model to
# - write one paragraph answering six questions: **who, what, when, where, why, how**,
# - use **only** the evidence given, never outside knowledge,
# - cite **transaction IDs** for every amount or date it states, and a **policy section** or **regulation** for the reason,
# - say plainly what is **missing** if the evidence cannot support a statement, instead of guessing,
# - describe the activity and the reasons for suspicion, not guilt.
#
# **user** holds the evidence (`render(b)` gives it as text) and the instruction to write the narrative.

# %%
def narrative_messages(b):
    #### SOLUTION narrative_messages
    system = ("You are an anti-money laundering analyst drafting the narrative of a suspicious transaction report. "
              "Write one short paragraph that answers who, what, when, where, why and how. "
              "Use ONLY the evidence in the user message. Cite the transaction IDs (like T0001234) for every amount and date you state, "
              "and cite the policy section (like 'policy section 8') or the regulation citation for the reason it is suspicious. "
              "If the evidence cannot support a statement, say what is missing instead of guessing. "
              "Describe the activity and why it is suspicious. Do not say that anyone is guilty.")
    return system, f"{render(b)}\n\nWrite the narrative for alert {b['alert']['id']}."
    #### STUDENT
    # TODO: a system message with the five requirements above
    # TODO: a user message that contains the rendered evidence and the instruction
    return "", ""
    #### END

# %%
system, user = narrative_messages(b)
for word in ("who", "what", "when", "where", "why", "how"):
    check(f"the system message asks for '{word}'", word in system.lower(), "name all six questions")
check("it limits the model to the evidence", "only" in system.lower() and "evidence" in system.lower(), "say ONLY the evidence")
check("it asks for transaction citations and a rule", "transaction" in system.lower() and ("policy section" in system.lower() or "regulation" in system.lower()), "ask for both")
check("it tells the model to report what is missing", "missing" in system.lower(), "avoid guessing")
check("the evidence is in the user message", b["alert"]["id"] in user and b["transactions"][0]["id"] in user, "include render(b)")

# %% [markdown]
# ## Draft, guard, and read the result
#
# Put the pieces together. With no `GROQ_API_KEY` the model is a mock and the draft is a placeholder, so the gate rightly **rejects** it.
# That is the point of Exercise 2. With a key, run it on several alerts and see how often a real model passes.

# %%
def draft(alert_id):
    bd = bundle(alert_id)
    system, user = narrative_messages(bd)
    reply = chat(system, user)
    return reply, check_citations(reply.text, bd)

reply, result = draft(escalate_ids[0])
print("provider:", reply.provider, "|", reply.text[:400].replace("\n", " "))
print(result)
print("acceptable:", acceptable(result))
if reply.provider == "mock":
    print("\n(mock provider: set GROQ_API_KEY in .env to see a real draft. The gate correctly refuses the placeholder.)")

# %% [markdown]
# ## A hand-built narrative, to see a pass
#
# This one is assembled from the bundle with no model. Every ID and section it cites comes from the evidence, so it passes.

# %%
def reference_narrative(bd):
    a, c = bd["alert"], bd["customer"]
    own = [t for t in bd["transactions"] if a["account"] in (t["from_account"], t["to_account"])]
    top = sorted(sorted(own, key=lambda t: -float(t["amount"]))[:4], key=lambda t: t["timestamp"])
    ids = ", ".join(t["id"] for t in top)
    sec = next((p["section"] for p in bd["policy"]), "8")
    reg = next((p["citation"] for p in bd["regulations"] if p.get("citation")), None)
    return (f"Alert {a['id']} on account {a['account']} ({bd['rule'].lower()}). Between {top[0]['timestamp'][:10]} and {top[-1]['timestamp'][:10]} "
            f"the account had large transfers ({ids}), against an expected activity of about {int(float(c['expected_monthly_activity'])):,} a month. "
            f"Policy section {sec} applies." + (f" See {reg}." if reg else ""))

ref = reference_narrative(b)
print(ref)
print(check_citations(ref, b), "->", "acceptable" if acceptable(check_citations(ref, b)) else "NOT acceptable")

# %% [markdown]
# ## Catch the mistake
#
# Below is a draft with four planted problems. Run your checker, then list in your answer what it caught and **what it could not**.

# %%
planted = (f"Alert {b['alert']['id']}: the customer received {real[0]} and {elsewhere}, then moved the funds on in {real[1]}. "
           f"The total of 250,000 is far above the profile. This breaches policy section 8 and PCMLTFA s. 999. See also T9999999.")
res = check_citations(planted, b)
print(res)
print("unsupported (real ID, wrong alert):", res["unsupported"])
print("invalid (does not exist):", res["invalid"])

# %% [markdown]
# **Your turn (write your answer):** the draft contains (1) a made-up transaction, (2) a real transaction from another customer, (3) a made-up
# regulation section, and (4) a claim that the total is 250,000. Which of the four does `check_citations` catch? Which does it miss, and
# what check would you write for the one it misses? (Think: every amount in the text must be an amount in the evidence, or a sum
# you can recompute.)
#
# *Your answer:*

# %% [markdown]
# ## What to take away
#
# - **Verify references, not prose.** A fluent paragraph proves nothing. IDs and sections can be checked against the data in a few
#   lines, and that check can run on every draft.
# - **A gate must fail on silence.** A check that a text "contains no invalid citations" is satisfied by a text with no citations.
# - **Valid is not the same as supported.** A real transaction ID from the wrong customer is the dangerous one, because it looks right.
# - What you built catches false *references*. It does not catch false *claims* (the 250,000). That needs separate checks and a human.
#
# **Next:** notebook 4 makes the decision (escalate, monitor, dismiss) and scores it against the answer key.
