# %% [markdown]
# # AML lab 4: decide, then measure honestly
#
# **Time:** about 35 minutes. **Cost:** nothing (a Groq key makes Exercise 3 real). **Needs:** AML labs 1 to 3.
#
# The investigator's decision on each alert is one of three: **escalate** (file a report), **monitor** (keep watching) or **dismiss**
# (explained and harmless). You build a rule that decides from the evidence bundle, a model-based decision with a strict parser, and a
# scorer, and then you look for **where they are wrong**. A single accuracy number hides that.
#
# ## Starting point
#
# Your corpus, search and bundle from labs 1 and 2. The next cell builds a bundle for every alert (a few seconds).

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
from collections import Counter
from labagent.ai.rag.labkit import chat

bundles = {aid: bundle(aid) for aid in alerts}
labels = Counter(truth[aid]["label"] for aid in alerts)
print(len(bundles), "bundles. Answer key:", dict(labels))
print("Always guessing the most common label would score", round(max(labels.values()) / len(alerts), 2))

# %% [markdown]
# That last line is your **baseline**. Any method that does not beat it is not deciding anything.
#
# ## Exercise 1: the scorer
#
# `score(decisions)` takes `{alert_id: label}` and compares it with the answer key. It returns
#
# - `decided`: how many of the given alerts are in the key (ignore unknown ids),
# - `agree`: how many match,
# - `agreement`: `agree / decided` rounded to 3 places, or `None` if nothing was decided,
# - `confusion`: a dict `{(key_label, your_label): count}` over the decided alerts,
# - `disagree`: `{alert_id: {"you": ..., "key": ...}}`.

# %%
def score(decisions):
    #### SOLUTION score
    known = {a: d for a, d in decisions.items() if a in truth}
    agree = sum(truth[a]["label"] == d for a, d in known.items())
    return {"decided": len(known), "agree": agree, "agreement": round(agree / len(known), 3) if known else None,
            "confusion": dict(Counter((truth[a]["label"], d) for a, d in known.items())),
            "disagree": {a: {"you": d, "key": truth[a]["label"]} for a, d in known.items() if truth[a]["label"] != d}}
    #### STUDENT
    # TODO: keep only alerts that are in `truth`; count agreement; build the confusion counts and the disagreements
    return {"decided": 0, "agree": 0, "agreement": None, "confusion": {}, "disagree": {}}
    #### END

# %%
ids = list(alerts)
perfect = {a: truth[a]["label"] for a in ids}
check("a perfect set of decisions scores 1.0", score(perfect)["agreement"] == 1.0 and score(perfect)["decided"] == len(ids), "compare each decision with truth[alert_id]['label']")
flip = {a: ("dismiss" if truth[a]["label"] != "dismiss" else "escalate") for a in ids}
check("all wrong scores 0.0 and lists every disagreement", score(flip)["agreement"] == 0.0 and len(score(flip)["disagree"]) == len(ids), "disagree holds every alert that does not match")
half = {**perfect, **{a: flip[a] for a in ids[: len(ids) // 2]}}
check("a partial set is a share", abs(score(half)["agreement"] - round(1 - (len(ids) // 2) / len(ids), 3)) < 1e-9, "agreement = agree / decided")
check("unknown alert ids are ignored", score({"AL99999": "escalate", ids[0]: truth[ids[0]]["label"]})["decided"] == 1, "only count alerts that are in the key")
check("nothing decided gives None", score({})["agreement"] is None, "avoid dividing by zero")
check("the confusion counts add up", sum(score(flip)["confusion"].values()) == len(ids), "one entry per decided alert")

# %% [markdown]
# ## Exercise 2: a rule that decides
#
# **Before you write a rule, read what the policy says.** Run the cell, read what comes back, and write your rule from it.

# %%
for h in search("When should an investigator escalate instead of monitor?", 2, doc_type="policy"):
    print(f"[{h['id']}] {h['text']}\n")
print(render(bundles["AL00001"])[:900])

# %% [markdown]
# `triage_rules(b)` takes one bundle and returns `"escalate"`, `"monitor"` or `"dismiss"`. Things the bundle can tell you:
#
# - `b["sanctions"]`: a screening hit with `match_type` (`exact` or a fuzzy kind). An exact name match to a sanctions list is not a judgement call.
# - `b["transactions"]` descriptions: a **house sale**, a **bonus** or **tuition** are ordinary explanations (`REAL ESTATE CLOSING PROCEEDS`, `PAYROLL BONUS`, `TUITION PAYMENT`).
# - The **ratio** of the alert's amount to the customer's `expected_monthly_activity`.
# - How many **uncommon** counterparties paid in or were paid out in the weeks before the alert (`b["uncommon_parties"]`).
#
# Aim for at least **75 percent agreement**, which beats the baseline by a wide margin. You will see in the next step why it is not 100.

# %%
BENIGN = {"REAL ESTATE CLOSING PROCEEDS", "PAYROLL BONUS", "TUITION PAYMENT"}

def triage_rules(b):
    """Return 'escalate', 'monitor' or 'dismiss' for one evidence bundle. Do not read the answer key."""
    #### SOLUTION triage_rules
    a, c, acct = b["alert"], b["customer"], b["alert"]["account"]
    if b["sanctions"]:
        return "escalate" if any(h["match_type"] == "exact" for h in b["sanctions"]) else "monitor"
    end = date.fromisoformat(a["date"])
    recent = [t for t in b["transactions"] if acct in (t["from_account"], t["to_account"]) and 0 <= (end - day(t)).days <= 21]
    if {t["description"] for t in recent} & BENIGN:
        return "dismiss"
    ratio = float(a["amount"]) / max(float(c["expected_monthly_activity"]), 1.0)
    unusual = set(b["uncommon_parties"])
    senders = {t["from_account"] for t in recent if t["to_account"] == acct} & unusual
    payees = {t["to_account"] for t in recent if t["from_account"] == acct} & unusual
    legs = [t for t in b["transactions"] if t["from_account"] in unusual | {acct} and t["to_account"] in unusual | {acct}]
    if ratio >= 1 and (len(senders) >= 3 or len(payees) >= 3 or len(legs) >= 3):
        return "escalate"
    return "monitor"
    #### STUDENT
    # TODO: sanctions first; then ordinary explanations; then ratio and counterparties; default to monitor
    return "monitor"
    #### END

# %%
mine = {aid: triage_rules(b) for aid, b in bundles.items()}
result = score(mine)
baseline = max(labels.values()) / len(alerts)
print(f"agreement {result['agreement']:.2f} on {result['decided']} alerts (baseline {baseline:.2f})")
check("every decision is one of the three labels", set(mine.values()) <= {"escalate", "monitor", "dismiss"}, "return exactly 'escalate', 'monitor' or 'dismiss'")
check("it beats the baseline by at least 0.2", result["agreement"] >= baseline + 0.2, "start with the sanctions and the benign explanations")
check("it reaches 0.75", result["agreement"] >= 0.75, "look at the disagreements below and add one rule at a time")

# %% [markdown]
# ## Where is it wrong?
#
# One number says how often. The **confusion matrix** says how: which true label gets which of your labels. In AML the two kinds of
# error are not equal. Missing a true escalation is a compliance failure; escalating a harmless case wastes an investigator's day.

# %%
labels_order = ["escalate", "monitor", "dismiss"]
header = "key \\ you"
print(f"{header:>10}" + "".join(f"{l:>10}" for l in labels_order))
for k in labels_order:
    print(f"{k:>10}" + "".join(f"{result['confusion'].get((k, y), 0):>10}" for y in labels_order))

# %% [markdown]
# ### Exercise 2b: group the errors by pattern
#
# `by_pattern(decisions)` returns `{pattern: (alerts, agree)}` using the `pattern` field of the answer key (fan_in, cycle, none, ...).
# Then print the patterns where you disagree most.

# %%
def by_pattern(decisions):
    #### SOLUTION by_pattern
    out = {}
    for a, d in decisions.items():
        if a in truth:
            n, ok = out.get(truth[a]["pattern"], (0, 0))
            out[truth[a]["pattern"]] = (n + 1, ok + (truth[a]["label"] == d))
    return out
    #### STUDENT
    # TODO: for each decided alert, add 1 to the pattern's count, and 1 more to the agree count if the decision matches
    return {}
    #### END

# %%
groups = by_pattern(mine)
check("every alert is counted once", sum(n for n, _ in groups.values()) == len(alerts), "one entry per alert")
check("agreement per pattern adds up", sum(ok for _, ok in groups.values()) == result["agree"], "ok counts the matches")
for p, (n, ok) in sorted(groups.items(), key=lambda kv: kv[1][1] / kv[1][0]):
    print(f"{p:28} {ok}/{n}")
print()
for aid, d in list(result["disagree"].items())[:4]:
    print(aid, d, "|", truth[aid]["reason"][:110])

# %% [markdown]
# **Your turn (write your answer):** which pattern is weakest? Read the `reason` of two of its alerts and the bundle for one
# (`print(render(bundles['AL000XX']))`). What does a human see that your rule does not, and what rule would capture it? Add it, rerun the
# scorer, and write down the new number.
#
# *Your answer:*
#
# ## Exercise 3: let a model decide, and parse it strictly
#
# A model's answer is text. If you search the text for the first label, "I would **not escalate** this; **monitor**" becomes
# "escalate". So make the format strict: ask for a last line `DECISION: <label>` and accept **only** that.
#
# Write `parse_label(text)`: return the label from the **last** line that matches `DECISION: escalate|monitor|dismiss` (any case), or
# `None` if there is no such line. Then write `triage_prompt(b)` returning `(system, user)`.

# %%
def parse_label(text):
    #### SOLUTION parse_label
    found = re.findall(r"^\s*DECISION:\s*(escalate|monitor|dismiss)\s*\.?\s*$", text or "", re.I | re.M)
    return found[-1].lower() if found else None
    #### STUDENT
    # TODO: re.findall over the lines for "DECISION: <label>"; return the last one in lower case, or None
    return None
    #### END

# %%
check("it reads the marker line", parse_label("Reasons...\nDECISION: escalate") == "escalate", "match DECISION: followed by a label")
check("it ignores a label in the prose", parse_label("I would not escalate this; monitor it.") is None, "only the DECISION line counts")
check("it takes the last marker", parse_label("DECISION: monitor\nOn reflection:\nDECISION: dismiss") == "dismiss", "use the last match")
check("case does not matter", parse_label("decision: ESCALATE") == "escalate", "ignore case and return lower case")
check("None in, None out", parse_label(None) is None and parse_label("") is None, "handle empty text")

# %%
def triage_prompt(b):
    #### SOLUTION triage_prompt
    system = ("You review an anti-money laundering alert for a credit union. Use ONLY the evidence in the user message. "
              "Choose one of: escalate (reasonable grounds to suspect: file a report), monitor (unusual but unexplained: keep watching), "
              "dismiss (a clear, ordinary explanation). Explain briefly and cite the transaction IDs and the policy section you relied on. "
              "If the evidence is not enough, say what is missing. End with a last line exactly like: DECISION: escalate (or monitor, or dismiss).")
    return system, f"{render(b)}\n\nDecide alert {b['alert']['id']}."
    #### STUDENT
    # TODO: define the three labels, require evidence-only reasoning with citations, and require the final DECISION line
    return "", ""
    #### END

# %%
system, user = triage_prompt(bundles["AL00005"])
check("the prompt defines all three labels", all(l in system.lower() for l in ("escalate", "monitor", "dismiss")), "name and define each label")
check("it requires the DECISION line", "DECISION:" in system, "tell the model the exact last line")
check("it limits the model to the evidence", "only" in system.lower() and "evidence" in system.lower(), "say ONLY the evidence")
check("the evidence and the alert id are in the user message", "AL00005" in user and bundles["AL00005"]["transactions"][0]["id"] in user, "include render(b)")

# %% [markdown]
# Now run the model on a small balanced set (4 alerts per label). With no key you get the mock provider, which cannot answer: you will
# see `None` for every alert, which is the strict parser doing its job. With a Groq key you get real decisions to score.

# %%
sample = sorted(a for lab_ in ("escalate", "monitor", "dismiss") for a in [x for x in alerts if truth[x]["label"] == lab_][:4])
model_says, providers = {}, set()
for aid in sample:
    system, user = triage_prompt(bundles[aid])
    reply = chat(system, user)
    providers.add(reply.provider)
    model_says[aid] = parse_label(reply.text)
usable = {a: d for a, d in model_says.items() if d}
print("provider:", providers, "| usable answers:", f"{len(usable)}/{len(sample)}")
if usable:
    print("model:", score(usable)["agreement"], "| your rule on the same alerts:", score({a: mine[a] for a in usable})["agreement"])
else:
    print("(no usable model answers: set GROQ_API_KEY in .env and run again)")

# %% [markdown]
# ## Compare with the packaged reference rule
#
# The pack ships a fixed reference rule that the launch uses to prove the lab is solvable. Run it on **your** bundles.

# %%
from labagent.packs.aml.rag.quality import triage_baseline
reference = {aid: triage_baseline(b) for aid, b in bundles.items()}
print("your rule     :", result["agreement"])
print("reference rule:", score(reference)["agreement"])

# %% [markdown]
# ## What this number means, and what it does not
#
# The reference rule scores close to 1.0. That does not mean AML triage is solved. **The labels come from how this data was built, not from an
# investigator.** A generator planted each pattern and wrote the label; the reference rule was written against the same patterns. So a high
# score shows that the evidence is *enough to decide on this invented data*, and nothing more.
#
# **Your turn (write your answer):** list three things a real credit union's alerts have that this data does not, and for each say how it would
# change what you measure. (Hints: disagreeing senior investigators, labels that arrive months later, drifting customer behaviour,
# rare true positives, and how many alerts are false alarms.) What would you need before you trusted a number like 0.9 here?
#
# *Your answer:*
#
# ## What to take away
#
# - **Beat a baseline first.** Always-guess-the-majority is the bar a method has to clear.
# - **Parse strictly.** A model's reasoning is free text; its decision should be a field you can validate.
# - **Look at errors, not the average.** The pattern table showed *where* the rule fails and what to fix.
# - **A synthetic benchmark checks your plumbing, not your model.** Use it to find bugs and leakage. Do not quote it as accuracy.
#
# ## Where to go next
#
# - Compare the pieces you wrote with the finished tool (`AmlLab`) and with `labagent/packs/aml/rag/` in the repository.
# - Run labs 1 to 4 again with `jurisdiction=both` or `size=medium` and see which of your checks change.
