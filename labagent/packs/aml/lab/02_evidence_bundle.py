# %% [markdown]
# # AML lab 2: build the evidence bundle
#
# **Time:** about 35 minutes. **Cost:** nothing. **Needs:** AML lab 1.
#
# A transaction monitor raises an **alert**: "this account did something unusual". An investigator cannot decide from the alert alone.
# They need the customer's profile, the transfers around the alert, the policy that applies, the sanctions screening, and how
# similar alerts were decided before. Gathering that is the **evidence bundle**, and it is the most important part of an AML RAG
# system: a model, or a person, can only decide from what the bundle contains.
#
# You build it in three steps and **measure** whether it is complete, using the answer key the pack kept apart.
#
# ## Setup
#
# Everything from lab 1 (the corpus, the store and your `search`) is here as working code. The tables are plain CSV files.

# %%
#### REFERENCE corpus_setup
#### REFERENCE store_setup
#### REFERENCE search

import csv
from collections import defaultdict
from datetime import date, timedelta

def table(name, folder="tables"):
    with open(root / folder / f"{name}.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

customers = {c["id"]: c for c in table("customers")}
accounts = {a["id"]: a for a in table("accounts")}
transactions = table("transactions")
alerts = {a["id"]: a for a in table("alerts")}
kyc = {k["customer_id"]: k["profile"] for k in table("kyc_profiles")}
sanctions = table("sanctions_hits")
print({n: len(v) for n, v in (("customers", customers), ("accounts", accounts), ("transactions", transactions), ("alerts", alerts))})
print(list(alerts.values())[0])
print(transactions[0])
#### SOLUTION bundle_setup
#### END

# %% [markdown]
# Things to notice in the tables:
# - An **alert** has a `rule` (R01 to R09), an `account`, a `date`, an `amount` and a `risk_score`. It does **not** say whether it
#   is a real problem. That is for the investigator.
# - **Transactions** run between accounts. Accounts starting with `ACC` are customers of the credit union. Others (names starting
#   with `X`) are outside parties: employers, shops, landlords, or other institutions.
# - The answer key is in `answer_key/ground_truth.csv`. It says, for every alert, the right decision, the pattern and the
#   **key transactions**. A bundle that leaves those out cannot support the right decision. You will use the key to test the bundle.
#   Nothing you build is allowed to read an alert's own answer.
#
# Helpers you do not need to write:

# %%
RULES = {
    "R01": "Cash deposits just under the reporting threshold", "R02": "Rapid movement of funds in and out",
    "R03": "Many senders to one account", "R04": "One account sends to many recipients",
    "R05": "Funds return to the sender through a loop", "R06": "Activity far above expected",
    "R07": "Wire to or from a high-risk country", "R08": "Round or just-under-threshold payments to new payees",
    "R09": "Sanctions name match",
}

by_account = defaultdict(list)                  # account id -> every transaction that touches it
partners = defaultdict(set)                     # outside party -> distinct accounts it deals with
for t in transactions:
    by_account[t["from_account"]].append(t)
    by_account[t["to_account"]].append(t)
    for me, other in ((t["from_account"], t["to_account"]), (t["to_account"], t["from_account"])):
        if me not in accounts:
            partners[me].add(other)

def is_common(party):
    """True for an ordinary outside party (an employer, a shop, a landlord): not one of our accounts, and it deals with 8 or more
    different accounts. Transfers through a common party say nothing about a link between two customers."""
    return party not in accounts and len(partners[party]) >= 8

def jurisdiction_of(alert):
    """'ca' or 'us'. In a lab with both countries, the currency of the account's transfers decides."""
    if cfg["jurisdiction"] != "both":
        return cfg["jurisdiction"]
    cur = next((t["currency"] for t in by_account[alert["account"]]), "CAD")
    return "us" if cur == "USD" else "ca"

def day(t):
    return date.fromisoformat(t["timestamp"][:10])
#### SOLUTION helpers
#### END

# %% [markdown]
# ## Exercise 1: the transactions around an alert
#
# `transactions_for(alert, days=30, hops=0)` returns the list of transactions for the alert, oldest first.
#
# - **hops=0:** only the transfers that touch the **alert's own account**, dated from `days` before the alert date up to the alert
#   date (both days included).
# - **hops=N:** also follow the money. Take the accounts that dealt with the alert account, then the accounts that dealt with
#   those, up to N steps out. For the accounts other than the alert account, keep a transfer only if **both ends are uncommon**
#   (`not is_common(...)`), and step out only into uncommon accounts. Without that rule, one shop would drag in thousands of unrelated customers.
#
# Start with `hops=0` and run the first check. Then measure how much of the story it misses, and add the hops.

# %%
def transactions_for(alert, days=30, hops=0):
    """Transactions for the alert in the window, oldest first. hops=0 is the alert account only."""
    #### SOLUTION transactions_for
    end = date.fromisoformat(alert["date"])
    start = end - timedelta(days=days)
    in_window = lambda t: start <= day(t) <= end
    seen, frontier, visited = {}, {alert["account"]}, {alert["account"]}
    for step in range(hops + 1):
        reached = set()
        for acc in frontier:
            for t in by_account[acc]:
                if not in_window(t):
                    continue
                other = t["to_account"] if t["from_account"] == acc else t["from_account"]
                if acc != alert["account"] and (is_common(acc) or is_common(other)):
                    continue
                seen[t["id"]] = t
                if other not in visited and not is_common(other):
                    visited.add(other)
                    reached.add(other)
        frontier = reached
    return sorted(seen.values(), key=lambda t: (t["timestamp"], t["id"]))
    #### STUDENT
    # TODO: start, end = the window around alert["date"]
    # TODO: for hops=0, collect by_account[alert["account"]] filtered to the window
    # TODO: for hops>0, repeat from the newly reached accounts (see the rules above)
    return []
    #### END

# %%
a0 = alerts["AL00005"]
own = transactions_for(a0, hops=0)
check("hops=0 returns only transfers of the alert account", own and all(a0["account"] in (t["from_account"], t["to_account"]) for t in own), "filter by_account[alert['account']]")
check("every transfer is inside the window", all(date.fromisoformat(a0["date"]) - timedelta(days=30) <= day(t) <= date.fromisoformat(a0["date"]) for t in own), "start = alert date minus days, end = alert date, both included")
check("oldest first", [t["timestamp"] for t in own] == sorted(t["timestamp"] for t in own), "sort by timestamp")
check("a shorter window gives fewer or equal rows", len(transactions_for(a0, days=5)) <= len(own), "use the days argument")

# %% [markdown]
# ### How complete is it?
#
# The answer key lists, for each alert, the **key transactions**: the ones that make it suspicious. *Recall* is the share of those
# the bundle contains. Measure it over every alert that should be escalated. (Reading the key here is allowed because we are testing
# the bundle, not deciding with it.)

# %%
truth = {g["alert_id"]: g for g in table("ground_truth", "answer_key")}      # ANSWER KEY: used only to test, never to build a bundle
escalate = [a for a in alerts.values() if truth[a["id"]]["label"] == "escalate"]
#### SOLUTION truth_setup
#### END

# %%
def key_recall(hops, days=30):
    recalls, sizes = [], []
    for a in escalate:
        keys = {k for k in truth[a["id"]]["key_transactions"].split(";") if k}
        got = {t["id"] for t in transactions_for(a, days, hops)}
        recalls.append(len(keys & got) / len(keys))
        sizes.append(len(got))
    return sum(recalls) / len(recalls), sum(sizes) / len(sizes), min(recalls)

for hops in (0, 1, 2, 5):
    r, size, worst = key_recall(hops)
    print(f"hops={hops}: recall {r:.2f}, worst alert {worst:.2f}, about {size:.0f} rows per bundle")

# %% [markdown]
# **Your turn (write your answer):** with `hops=0` you miss about a quarter of the key transactions. Which laundering patterns lose
# the most (print `truth[a['id']]['pattern']` for the alerts with low recall)? Why can't the alert account's own transfers show a
# *stack* or a *cycle*? And why not just set `hops=50`?
#
# *Your answer:*

# %%
r5, size5, worst5 = key_recall(5)
check("with hops the bundle holds at least 95 percent of the key transactions", r5 >= 0.95, f"recall is {r5:.2f}: follow the money one step further and keep uncommon parties only")
check("and it stays small enough to read", size5 <= 60, f"{size5:.0f} rows per bundle on average: skip transfers through common parties")
check("more hops never loses rows", all(len(transactions_for(a, 30, 2)) <= len(transactions_for(a, 30, 5)) for a in escalate[:5]), "each hop adds, it does not replace")

# %% [markdown]
# ## Exercise 2: similar earlier cases
#
# An investigator asks: "have we seen this before, and what did we decide?" `similar_cases(alert, n=3)` returns the `n` most recent
# **earlier** alerts that fired the same rule, oldest first, each as `{"alert_id", "date", "decision", "reason"}` taken from the
# answer key.
#
# This is the one place where the key is allowed in a bundle, because those decisions were already made before this alert. The danger
# is leaking the **current** alert's own answer or a later one. Your function must not.

# %%
def similar_cases(alert, n=3):
    """The n latest earlier alerts with the same rule (strictly before this alert's date), oldest first."""
    #### SOLUTION similar_cases
    earlier = sorted((a for a in alerts.values() if a["rule"] == alert["rule"] and a["date"] < alert["date"]), key=lambda a: (a["date"], a["id"]))[-n:]
    return [{"alert_id": a["id"], "date": a["date"], "decision": truth[a["id"]]["label"], "reason": truth[a["id"]]["reason"]} for a in earlier]
    #### STUDENT
    # TODO: alerts with the same rule and a strictly earlier date; keep the last n; look up decision and reason in `truth`
    return []
    #### END

# %%
probe = max(alerts.values(), key=lambda a: a["date"])
got = similar_cases(probe)
check("at most n results", len(got) <= 3 and got, "keep the last n")
check("all earlier than the alert", all(g["date"] < probe["date"] for g in got), "strictly before the alert date")
check("never the alert itself", all(g["alert_id"] != probe["id"] for g in got), "exclude the current alert")
check("same rule only", all(alerts[g["alert_id"]]["rule"] == probe["rule"] for g in got), "match on alert['rule']")
check("oldest first", [g["date"] for g in got] == sorted(g["date"] for g in got), "sort ascending by date")
first = min(alerts.values(), key=lambda a: a["date"])
check("the first alert has no history", similar_cases(first) == [], "nothing is earlier than the earliest alert")

# %% [markdown]
# ## Exercise 3: assemble the bundle
#
# `bundle(alert_id)` returns a dict with these keys:
#
# | key | content |
# |---|---|
# | `alert` | the alert row |
# | `rule` | the rule's plain-English name (`RULES`) |
# | `customer`, `kyc` | the customer row and the KYC profile text |
# | `jurisdiction` | `jurisdiction_of(alert)` |
# | `sanctions` | the sanctions hits for this customer |
# | `transactions` | `transactions_for(alert, hops=5)` |
# | `uncommon_parties` | accounts in those transactions that are not common parties, excluding the alert account |
# | `policy` | the top 3 **policy** chunks for the rule, from your `search`, restricted to the right country |
# | `regulations` | the top 3 **regulation** chunks (only if `cfg["regulations"]` is true) |
# | `similar_cases` | `similar_cases(alert)` |
#
# For `policy` and `regulations`, search with a question built from the rule, for example `f"{rule}: what does the policy say?"`.
# Customer: `customers[accounts[alert["account"]]["customer_id"]]`.

# %%
def bundle(alert_id):
    """Everything a reviewer needs to decide on one alert. Never reads the alert's own answer."""
    #### SOLUTION bundle
    alert = alerts[alert_id]
    j = jurisdiction_of(alert)
    rule = RULES[alert["rule"]]
    customer = customers[accounts[alert["account"]]["customer_id"]]
    rows = transactions_for(alert, hops=5)
    parties = {p for t in rows for p in (t["from_account"], t["to_account"])} - {alert["account"]}
    return {
        "alert": alert, "rule": rule, "customer": customer, "kyc": kyc[customer["id"]], "jurisdiction": j,
        "sanctions": [h for h in sanctions if h["customer_id"] == customer["id"]],
        "transactions": rows, "uncommon_parties": sorted(p for p in parties if not is_common(p)),
        "policy": search(f"{rule}: what does the policy say?", 3, doc_type="policy", jurisdiction=j),
        "regulations": search(f"{rule} reporting requirement", 3, doc_type="regulation", jurisdiction=j) if cfg["regulations"] else [],
        "similar_cases": similar_cases(alert),
    }
    #### STUDENT
    # TODO: look up the alert, its customer, and the jurisdiction
    # TODO: fill every key in the table above
    return {}
    #### END

# %% [markdown]
# Run the checks. They test the bundle's **shape**, its **sources**, and (most important) that it **does not leak** the alert's own
# answer.

# %% [markdown]
# **A canary test for leaks.** Comparing text is not enough, because many alerts share the same reason. So the check below plants a
# unique marker in this alert's own row of the answer key, builds the bundle, and fails if the marker appears anywhere in it. If
# your bundle ever copies the alert's own answer, the canary shows up.

# %%
def leaks_own_answer(alert_id):
    row = truth[alert_id]
    saved = dict(row)
    row.update(reason=f"CANARY-REASON-{alert_id}", label="CANARY-LABEL", pattern="CANARY-PATTERN", key_transactions="CANARY-KEYS")
    try:
        return "CANARY" in json.dumps(bundle(alert_id), default=str)
    finally:
        row.update(saved)

# %%
sample = [a["id"] for a in escalate[:3]] + [a["id"] for a in alerts.values() if truth[a["id"]]["label"] == "dismiss"][:2]
expected_keys = {"alert", "rule", "customer", "kyc", "jurisdiction", "sanctions", "transactions", "uncommon_parties", "policy", "regulations", "similar_cases"}
for aid in sample:
    b = bundle(aid)
    check(f"{aid}: every key is present", expected_keys <= set(b), f"missing {sorted(expected_keys - set(b))}")
    check(f"{aid}: the customer owns the alert account", b["customer"]["id"] == accounts[b["alert"]["account"]]["customer_id"], "follow alert -> account -> customer")
    check(f"{aid}: policy chunks are policy and in the right country", len(b["policy"]) == 3 and all(p["doc_type"] == "policy" and p["jurisdiction"] == b["jurisdiction"] for p in b["policy"]), "filter by doc_type and jurisdiction")
    check(f"{aid}: sanctions hits belong to this customer", all(h["customer_id"] == b["customer"]["id"] for h in b["sanctions"]), "match on customer_id")
    check(f"{aid}: the alert's own answer is not in the bundle", not leaks_own_answer(aid) and not any(sc["alert_id"] == aid for sc in b["similar_cases"]),
          "similar_cases must only use earlier alerts, and nothing may copy the key row for this alert")
b = bundle(sample[0])
check("regulation chunks are present when the lab has them", bool(b["regulations"]) == bool(cfg["regulations"]), "only search regulations when cfg['regulations'] is true")
print("all bundles pass")

# %% [markdown]
# ## Read one, then compare it with the finished tool
#
# This is what a reviewer (or a model) will see. Then compare yours with `AmlLab.evidence`: they use the same ideas, so they should
# share most of their transactions.

# %%
def render(b, rows=25):
    a, c = b["alert"], b["customer"]
    out = [f"ALERT {a['id']} on {a['date']}: {b['rule']} (rule {a['rule']}), amount {a['amount']}, score {a['risk_score']}", f"CUSTOMER {c['id']}: {b['kyc']}"]
    out += [f"SANCTIONS: {h['match_type']} match to {h['sdn_name']} (score {h['score']})" for h in b["sanctions"]]
    out += ["POLICY:"] + [f"  [{p['id']}] {p['text'][:200]}" for p in b["policy"]]
    out += ["REGULATIONS:"] + [f"  [{p.get('citation')}] {p['text'][:160]}" for p in b["regulations"]]
    out += ["TRANSACTIONS:"] + [f"  {t['id']} {t['timestamp']} {t['from_account']} -> {t['to_account']} {t['amount']} {t['currency']} {t['channel']} {t['description']}" for t in b["transactions"][-rows:]]
    out += ["SIMILAR EARLIER CASES:"] + [f"  {s['alert_id']} ({s['date']}): {s['decision']}. {s['reason']}" for s in b["similar_cases"]]
    return "\n".join(out)
#### SOLUTION render_setup
#### END

# %%
print(render(bundle(sample[0])))

from aml_tools import AmlLab
finished = AmlLab(root)
mine = {t["id"] for t in bundle(sample[0])["transactions"]}
theirs = {t["id"] for t in finished.evidence(sample[0])["transactions"]}
print(f"\nyour bundle: {len(mine)} transfers | finished tool: {len(theirs)} | in both: {len(mine & theirs)}")

# %% [markdown]
# ## What to take away
#
# - **The bundle sets the ceiling.** If the key transfers are not in it, no prompt and no model can recover them. Measure the bundle
#   (recall against known-bad cases) before you tune anything downstream.
# - **Follow the money, but cut the noise.** Every extra hop adds evidence and also adds unrelated rows. The common-party rule is what
#   keeps the bundle readable.
# - **Leakage is a design bug, not a typo.** Using past decisions is useful; using this alert's own answer makes every later score
#   meaningless. Write the test for it first.
# - The recall you just measured is on invented data where the key transactions are known. On a real bank there is no key: the
#   equivalent is a sample of past investigations reviewed by a senior investigator.
#
# **Next:** notebook 3 checks that what is written about an alert is true: every transaction ID and regulation section cited must exist.
