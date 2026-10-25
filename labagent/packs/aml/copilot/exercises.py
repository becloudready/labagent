"""The exercises. Learner text is plain English with no technical terms. Expected answers are computed from the data, never typed in,
so they stay right when the seed, the size or the country changes. Each of the four sections ends with a catch-the-mistake exercise:
a short AI-style draft with planted errors that the learner finds by checking it against the files."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..corpus.documents import FACTS
from ..schema import Tables

SECTIONS = ("Policies", "Regulations", "Cases", "Data")
OTHER_JOB = {"teacher": "nurse", "nurse": "teacher", "student": "retired person", "retired": "student", "accountant": "plumber", "plumber": "accountant"}


def money(x: float) -> str:
    return f"{x:,.2f}"


def case_facts(t: Tables, case: Dict[str, Any]) -> Dict[str, Any]:
    a, g = case["alert"], case["truth"]
    tx = {x["id"]: x for x in t["transactions"]}
    keys = [tx[k] for k in g["key_transactions"].split(";") if k]
    acct = {x["id"]: x for x in t["accounts"]}[a["account"]]
    cust = {c["id"]: c for c in t["customers"]}[acct["customer_id"]]
    total = sum(float(x["amount"]) for x in keys)
    parties = {p for x in keys for p in (x["from_account"], x["to_account"])} - {a["account"]}
    days = sorted(x["timestamp"][:10] for x in keys)
    return {"alert": a, "truth": g, "customer": cust, "keys": keys, "total": total, "parties": len(parties), "first": days[0], "last": days[-1],
            "count": len(keys), "currency": keys[0]["currency"], "expected": int(cust["expected_monthly_activity"]),
            "ratio": total / max(float(cust["expected_monthly_activity"]), 1.0)}


def _first(cases: List[Dict[str, Any]], pred) -> Optional[Dict[str, Any]]:
    return next((c for c in cases if pred(c["truth"])), None)


def build_exercises(t: Tables, cases: List[Dict[str, Any]], jurisdiction: str, cohort: str) -> List[Dict[str, Any]]:
    j = "us" if jurisdiction == "us" else "ca"
    f = FACTS[j]
    report = f["suspicious"]
    home = "USD" if j == "us" else "CAD"
    year = min(x["timestamp"][:4] for x in t["transactions"])
    ex: List[Dict[str, Any]] = []

    # ---- Policies -----------------------------------------------------------------------------------------------------------------
    ex.append(dict(id="A1", section="Policies", title="Summarize the policy",
                   steps=["Open the file Policies > AML Policy Manual in Word.", "Ask Copilot: Summarize this document in five bullet points.",
                          "Read each bullet. Find the section in the manual that supports it."],
                   check=["Every bullet should match a numbered section. Write down any bullet you cannot find."],
                   expected=["Five bullets that cover: who owns the program, knowing the customer, monitoring alerts, reporting suspicious activity, and keeping records.",
                             "Each bullet maps to a section of the manual (sections 2, 3, 5, 8 and 9)."]))
    ex.append(dict(id="A2", section="Policies", title="Find the cash rule",
                   steps=["Stay in the AML Policy Manual.", "Ask Copilot: Which section covers cash deposits just under 10,000?",
                          "Open the section it names and read it."],
                   check=["Does the section Copilot names really talk about cash deposits near the limit?"],
                   expected=["Section 6, Cash and structuring.", f"It says to file a {f['cash']} when the credit union {f['cash_rule']}, and to treat several deposits just under 10,000 as a red flag."]))
    ex.append(dict(id="A3", section="Policies", title="Catch the AI's mistake: policy summary",
                   steps=["Read the draft below. An AI wrote it. It has two mistakes.", "Check each sentence against the AML Policy Manual.", "Write down the two mistakes and the section that proves each one."],
                   check=["Look at numbers and time periods first. They are the easiest things for an AI to get wrong."],
                   draft=("The program owner reports to the Board every month. Staff must verify the identity of every new customer before the first transaction. "
                          "Records must be kept for three years. A second investigator reviews every decision to escalate."),
                   expected=["Mistake 1: the owner reports to the Board each quarter, not every month (section 2).",
                             f"Mistake 2: records are kept for {f['records_time']}, not three years (section 9)."]))

    # ---- Regulations --------------------------------------------------------------------------------------------------------------
    ex.append(dict(id="B1", section="Regulations", title="Explain the cash report rule",
                   steps=["Open the file in Regulations that covers cash reports.", "Ask Copilot: Explain in plain words when a financial institution must report a cash transaction, and by when.",
                          "Compare its answer with the exact words in the file."],
                   check=["Does Copilot give the amount, the word that goes with it (more than, or at least), and the deadline?"],
                   expected=[f"The rule applies when the institution {f['cash_rule']}. The report is due {f['cash_time']}.", f"Source: {f['cash_cite']} and {f['cash_time_cite']}."]))
    more = "$10,000 or more" if j == "ca" else "more than $10,000"
    wrong = "more than $10,000" if j == "ca" else "$10,000 or more"
    ex.append(dict(id="B2", section="Regulations", title="Catch the AI's mistake: the cash rule",
                   steps=["Read the draft below. An AI wrote it. It has two mistakes.", "Check it against the file in Regulations.", "Write down both mistakes and the exact words from the file."],
                   check=["One mistake is a small word that changes the rule. The other is a number of days."],
                   draft=f"A report is needed when the institution handles {wrong} in cash. The report must be sent within 30 days.",
                   expected=[f"Mistake 1: the amount is {more}, not {wrong}.", f"Mistake 2: the report is due {f['cash_time']}, not within 30 days.", f"Source: {f['cash_cite']}, {f['cash_time_cite']}."]))

    # ---- Cases --------------------------------------------------------------------------------------------------------------------
    graph = _first(cases, lambda g: g["label"] == "escalate" and g["pattern"] not in ("sanctions_match", "disguised_payroll", "disguised_supplies"))
    other = _first(cases, lambda g: g["label"] == "escalate" and g["pattern"] in ("disguised_payroll", "disguised_supplies")) or graph
    sanc = _first(cases, lambda g: g["pattern"] == "sanctions_match" and g["label"] == "escalate")
    look = _first(cases, lambda g: g["label"] == "monitor" and g["pattern"].startswith("possible_"))
    if graph:
        n = case_facts(t, graph)
        ex.append(dict(id="C1", section="Cases", title="Summarize a case and list the red flags", case=graph["number"],
                       steps=[f"Open the workbook for Case {graph['number']:02d} in the Cases folder.", f"Ask Copilot: Summarize this case and list the red flags.",
                              "Open the Transactions tab and check each red flag against the rows."],
                       check=["A red flag must point to rows you can see. If Copilot names one you cannot find, mark it as unproven."],
                       expected=[graph["truth"]["reason"], f"Rows to find on the Transactions tab: {', '.join(x['id'] for x in n['keys'][:8])}{' and more' if len(n['keys']) > 8 else ''}.",
                                 f"The customer profile expects about {n['expected']:,} {n['currency']} a month. The listed transfers add up to {money(n['total'])}."]))
    if other:
        n = case_facts(t, other)
        ex.append(dict(id="C2", section="Cases", title=f"Draft a {report} narrative and check it", case=other["number"],
                       steps=[f"Open the workbook and the Word file for Case {other['number']:02d}.", f"Ask Copilot: Draft a {report} narrative for this case. Say who, what, when, where, why and how.",
                              "Check every fact in the draft against the Transactions tab. Fix any fact that is wrong."],
                       check=["Every amount, date and transaction ID in the narrative must match a row. Add up the totals yourself."],
                       expected=[f"Customer: {n['customer']['name']}, account {other['alert']['account']}.", f"Dates: {n['first']} to {n['last']}. Transfers listed: {n['count']}. They add up to {money(n['total'])} {n['currency']}. Money that passes through an account is counted each time it moves.",
                                 f"Profile: the customer is expected to handle about {n['expected']:,} {n['currency']} a month.", f"Why suspicious: {other['truth']['reason']}",
                                 f"Decision: {other['truth']['label']}. Policy section 8. Rule: {f['suspicious_cite']}."]))
    if sanc:
        n = case_facts(t, sanc)
        ex.append(dict(id="C3", section="Cases", title="Find the name match", case=sanc["number"],
                       steps=["Open the Cases folder. Select up to five case workbooks.", "Ask Copilot: Which of these customers has a name that matches a sanctions list entry?",
                              "Open the workbook it names and read the Summary and KYC tabs."],
                       check=["Copilot can only look at five files at a time. If the match is not in your five, pick five others."],
                       expected=[f"Case {sanc['number']:02d}: {n['customer']['name'].rstrip('.')}.", sanc["truth"]["reason"]]))
    if look:
        n = case_facts(t, look)
        job = OTHER_JOB.get(n["customer"]["occupation"], "student")
        draft = (f"Case {look['number']:02d} is about {n['customer']['name']}, who works as a {job}. Between {n['first']} and {n['last']} the account had {n['count'] + 2} transfers "
                 f"totalling {money(n['total'] * 2)} {n['currency']}. The case should be escalated.")
        ex.append(dict(id="C4", section="Cases", title="Catch the AI's mistake: case summary", case=look["number"],
                       steps=[f"Read the draft below. An AI wrote it. It has three mistakes.", f"Open the workbook for Case {look['number']:02d} and check each fact.", "Write down the three mistakes with the right facts."],
                       check=["Check the occupation on the KYC tab, then count and add up the rows on the Transactions tab."],
                       draft=draft,
                       expected=[f"Mistake 1: the customer is described in the file as: {n['customer']['occupation']}.", f"Mistake 2: the case covers {n['count']} transfers, not {n['count'] + 2}.",
                                 f"Mistake 3: the total is {money(n['total'])} {n['currency']}, not {money(n['total'] * 2)}.", f"The decision in the answer key is {look['truth']['label']}, not escalate."]))

    # ---- Data ---------------------------------------------------------------------------------------------------------------------
    internal = {a["id"] for a in t["accounts"]}
    owner = {a["id"]: a["customer_id"] for a in t["accounts"]}
    names = {c["id"]: c["name"] for c in t["customers"]}
    rows = sorted(t["transactions"], key=lambda x: (x["timestamp"], x["id"]))
    seen, new_payee = set(), []
    for x in rows:
        pair = (x["from_account"], x["to_account"])
        if x["from_account"] in internal and x["timestamp"].startswith(f"{year}-03") and float(x["amount"]) > 5000 and pair not in seen:
            new_payee.append(x)
        seen.add(pair)
    ex.append(dict(id="D1", section="Data", title="Find large transfers to new payees", steps=[
        "Open Data > Transactions in Excel. Turn on AutoSave.", "Ask Copilot: Find all transfers over 5,000 to new payees in March. A new payee is an account the sender has not paid before in this file.",
        "Check the list. Does Copilot say how it decided a payee was new?"], check=["To call a payee new, you must look at earlier months too. If the file is too big for Copilot, use the file for one month in Data > Transactions by month and say which payees are new within that month only."],
        expected=[f"{len(new_payee)} transfers over 5,000 went to a payee the sender had not paid earlier in the data.", f"First five: {', '.join(x['id'] for x in new_payee[:5])}."
                  ]))
    cash: Dict[str, float] = {}
    for x in rows:
        if x["channel"] == "cash" and x["to_account"] in internal and x["currency"] == home:
            cash[names[owner[x["to_account"]]]] = cash.get(names[owner[x["to_account"]]], 0.0) + float(x["amount"])
    top = sorted(cash.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ex.append(dict(id="D2", section="Data", title="Rank customers by cash deposits", steps=[
        "Open Data > Transactions in Excel. Turn on AutoSave.", f"Ask Copilot: Which five customers received the most cash deposits in {home}? Show the total for each.",
        "Check one total yourself with a filter on the Channel column."], check=["Only count rows where Channel is cash and the money went to a customer account."],
        expected=[f"{i}. {n}: {money(v)} {home}" for i, (n, v) in enumerate(top, 1)]))
    by_channel: Dict[str, int] = {}
    by_month: Dict[str, int] = {}
    for x in rows:
        by_channel[x["channel"]] = by_channel.get(x["channel"], 0) + 1
        by_month[x["timestamp"][:7]] = by_month.get(x["timestamp"][:7], 0) + 1
    busiest = max(sorted(by_month), key=lambda m: by_month[m])
    ex.append(dict(id="D3", section="Data", title="Chart activity by channel", steps=[
        "Open Data > Transactions in Excel. Turn on AutoSave.", "Ask Copilot: Make a chart of the number of transfers each month, split by channel.", "Check that the chart has twelve months and five channels."],
        check=["Hover over one bar and compare its number with a count you make using a filter."],
        expected=["Transfers by channel: " + ", ".join(f"{k} {v:,}" for k, v in sorted(by_channel.items(), key=lambda kv: -kv[1])) + ".", f"Busiest month: {busiest} with {by_month[busiest]:,} transfers."]))
    et = [float(x["amount"]) for x in rows if x["channel"] == "e-transfer" and x["currency"] == home]
    avg = sum(et) / len(et)
    ex.append(dict(id="D4", section="Data", title="Catch the AI's mistake: the numbers", steps=[
        "Read the draft below. An AI wrote it. It has two mistakes.", f"In Data > Transactions, filter Channel to e-transfer and Currency to {home}.", "Work out the real count and the real average. Write down both."],
        check=["Select the Amount column and read the Count and Average at the bottom of Excel. Then try the AVERAGE formula."],
        draft=f"There are {len(et) + 1_000:,} e-transfers in {home}, and the average e-transfer is {money(avg * 1.35)} {home}.",
        expected=[f"Mistake 1: the count is {len(et):,}, not {len(et) + 1000:,}.", f"Mistake 2: the average is {money(avg)} {home}, not {money(avg * 1.35)}."]))
    return ex
