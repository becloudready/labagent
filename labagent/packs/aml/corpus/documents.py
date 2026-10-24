"""Documents written for the invented institution: the AML policy manual, the alert handling procedure, the report-writing guide,
and one case file per alert.

Every regulation citation here is checked offline against data/reg_index.json (see tests). Numbers that come from the real
rules (the 10,000 cash threshold, 15 days, 30 days, five years) were read from the files in that index, not from memory.
Case files never include the label, pattern or reason from ground_truth: those are the answers.

Writing rules: short sentences, plain words, action verbs, no hype, no em dashes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ...rag.chunking import make_chunks
from ..schema import INSTITUTION, RULES, Tables

JURISDICTIONS = {"ca": "Canada", "us": "United States"}


@dataclass
class Section:
    id: str
    heading: str
    paragraphs: List[str]


@dataclass
class Doc:
    id: str
    title: str
    kind: str                  # policy | procedure | guide | case
    jurisdiction: str
    sections: List[Section] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


# ---- jurisdiction facts: each one is quoted from the rule it cites --------------------------------------------------------------

FACTS = {
    "ca": dict(
        regulator="FINTRAC", law="the Proceeds of Crime (Money Laundering) and Terrorist Financing Act (PCMLTFA)", suspicious="Suspicious Transaction Report (STR)",
        cash="Large Cash Transaction Report (LCTR)", cash_rule="receives $10,000 or more in cash in a single transaction",
        cash_cite="PCMLTF Regulations s. 10", cash_time="within 15 days after the day the credit union receives the cash", cash_time_cite="PCMLTF Regulations s. 132",
        id_cite="PCMLTFA s. 6.1", pep_cite="PCMLTFA s. 9.3", program_cite="PCMLTFA s. 9.6", suspicious_cite="PCMLTFA s. 7",
        suspicious_time="as soon as practicable after the credit union has taken the measures that let it establish reasonable grounds to suspect",
        suspicious_time_cite="PCMLTF STR Regulations s. 9", records_cite="PCMLTF Regulations s. 148", records_time="at least five years",
        records_cite2="PCMLTFA s. 6", confidentiality_cite="PCMLTFA s. 8", officer="Chief Anti-Money Laundering Officer",
        suspicion="reasonable grounds to suspect", structuring=("FINTRAC lists deposits structured to stay under a reporting threshold as a money laundering indicator.", None),
        report_short="STR", currency="CAD"),
    "us": dict(
        regulator="FinCEN", law="the Bank Secrecy Act", suspicious="Suspicious Activity Report (SAR)",
        cash="Currency Transaction Report (CTR)", cash_rule="handles a transaction in currency of more than $10,000",
        cash_cite="31 CFR 1010.311", cash_time="within 15 days following the day the transaction occurred", cash_time_cite="31 CFR 1010.306",
        id_cite="31 CFR 1020.220", pep_cite="31 CFR 1010.230", program_cite="31 CFR 1020.210", suspicious_cite="31 CFR 1020.320",
        suspicious_time="no later than 30 calendar days after the date of initial detection of facts that may constitute a basis for filing",
        suspicious_time_cite="31 CFR 1020.320", records_cite="31 CFR 1010.430", records_time="five years",
        records_cite2="31 CFR 1010.410", confidentiality_cite="31 CFR 1020.320", officer="BSA Officer",
        suspicion="know, suspect, or have reason to suspect", structuring=("The rules forbid structuring a transaction to evade the reporting requirements.", "31 CFR 1010.314"),
        report_short="SAR", currency="USD"),
}


def _p(*s: str) -> List[str]:
    return list(s)


def policy_manual(j: str) -> Doc:
    f = FACTS[j]
    rules = [f"{k}: {v}." for k, v in RULES.items()]
    struct, struct_cite = f["structuring"]
    struct_line = struct + (f" See {struct_cite}." if struct_cite else "")
    secs = [
        Section("1", "Purpose and scope", _p(
            f"This manual sets the anti-money laundering rules for {INSTITUTION}. It applies to every employee, branch and product.",
            f"The credit union follows {f['law']}. The reporting body is {f['regulator']}. Staff must read this manual every year.")),
        Section("2", "Roles", _p(
            f"The {f['officer']} owns this program and reports to the Board each quarter.",
            "Front-line staff spot unusual activity and pass it on the same day. Investigators review alerts and write the decision. "
            "A second investigator reviews every decision to escalate.")),
        Section("3", "Know your customer", _p(
            f"Verify the identity of every new customer before the first transaction. The rule is in {f['id_cite']}.",
            "Record the customer's name, address, occupation or industry, and the source of funds. "
            "Write a short profile with the activity you expect each month. The profile is the yardstick for every alert.")),
        Section("4", "Customer due diligence and risk rating", _p(
            "Give every customer a risk rating of low, medium or high when the account opens. Review it each year, and again when activity changes.",
            "Raise the rating for cash-intensive businesses, customers in high-risk countries, and customers with complex ownership. "
            f"Take extra steps for politically exposed persons and for owners of legal entities. See {f['pep_cite']}.")),
        Section("5", "Transaction monitoring", _p(
            "The monitoring system raises an alert when activity matches a rule. An alert is not a finding. It is a question to answer.",
            "The rules in use are:", *rules,
            "Compare each alert with the customer profile. Activity far above the expected monthly amount is the strongest sign that something is wrong.")),
        Section("6", "Cash and structuring", _p(
            f"File a {f['cash']} when the credit union {f['cash_rule']}. File it {f['cash_time']}. See {f['cash_cite']} and {f['cash_time_cite']}.",
            struct_line,
            "Treat several cash deposits just under 10,000 in a short time as a red flag, even when each deposit is below the threshold.")),
        Section("7", "Sanctions screening", _p(
            "Screen every customer name against the sanctions lists at onboarding and each time a list changes.",
            "An exact match to a listed name is escalated at once. A close but not identical match is monitored until the investigator compares "
            "registration details and ownership. Never tell the customer about a possible match.")),
        Section("8", "Escalating and reporting suspicious activity", _p(
            f"Escalate when you {f['suspicion']} that activity involves money laundering or other crime. The reporting rule is in {f['suspicious_cite']}.",
            f"File a {f['suspicious']} {f['suspicious_time']}. See {f['suspicious_time_cite']}.",
            "Escalate when the facts show a clear pattern, such as many accounts sending to one account, or funds that leave within days of arriving. "
            "Monitor when the facts are unusual but have a plausible reason. Dismiss when the facts have a documented reason.")),
        Section("9", "Record keeping", _p(
            f"Keep records for {f['records_time']}. See {f['records_cite']}. Keep the alert, the evidence you used and the decision.",
            f"Keep records of funds transfers and large cash transactions as the rules require. See {f['records_cite2']}.")),
        Section("10", "Confidentiality", _p(
            f"Never tell a customer or any third party that a {f['report_short']} was filed or is being considered. See {f['confidentiality_cite']}.",
            "Share case files only with staff who need them.")),
        Section("11", "Program review and training", _p(
            f"The program must be risk-based and effective. See {f['program_cite']}.",
            "Train every new employee within 30 days of the start date. Test the program each year and report the results to the Board.")),
    ]
    return Doc(f"policy-{j}", f"{INSTITUTION} AML Policy Manual ({JURISDICTIONS[j]})", "policy", j, secs)


def alert_procedure(j: str) -> Doc:
    f = FACTS[j]
    secs = [
        Section("1", "When to use this procedure", _p("Use this procedure for every alert from the monitoring system, whatever the score.")),
        Section("2", "Step 1: triage", _p(
            "Open the alert within two business days. Read the rule that fired, the account, the date and the amount.",
            "Check whether the account has other alerts in the last 12 months.")),
        Section("3", "Step 2: gather the evidence", _p(
            "Collect five things before you decide:",
            "1. The policy section that applies to the rule.",
            "2. The customer profile: occupation or industry, country, risk rating and expected monthly activity.",
            "3. The alert trigger: what the rule saw and when.",
            "4. The transactions: every transfer on the account for 30 days before the alert, and every transfer between the accounts involved.",
            "5. Similar closed cases, so your decision is consistent.")),
        Section("4", "Step 3: choose a decision", _p(
            "Escalate: the facts show a pattern that fits laundering and the customer profile does not explain it. Send the case to a second investigator.",
            "Monitor: the facts are unusual but a plausible reason exists. Set a review date and list what you still need.",
            "Dismiss: the facts have a documented reason, for example a house sale with the closing papers on file. Say which document.")),
        Section("5", "Step 4: write the note", _p(
            "Write the note in four parts: what happened, why it is unusual for this customer, which policy section applies, and your decision.",
            "Cite transaction IDs for every fact. A reader must be able to check each fact in the records.")),
        Section("6", "Step 5: review and close", _p(
            f"A second investigator reviews every escalation. If they agree, the {f['officer']} decides whether to file a {f['suspicious']}.",
            f"Keep the case file for {f['records_time']}. See {f['records_cite']}.")),
    ]
    return Doc(f"procedure-{j}", f"{INSTITUTION} Alert Handling Procedure ({JURISDICTIONS[j]})", "procedure", j, secs)


def report_guide(j: str) -> Doc:
    f = FACTS[j]
    secs = [
        Section("1", "What the narrative is for", _p(
            f"The narrative explains why the credit union suspects crime. It goes in the {f['suspicious']} sent to {f['regulator']}.",
            "Write it so a reader who has never seen the case can follow it.")),
        Section("2", "Answer six questions", _p(
            "Who: name the customer and the other parties, with account numbers.",
            "What: describe the activity in one or two sentences.",
            "When: give the dates, and the time span.",
            "Where: name the branch, channel or country.",
            "Why it is suspicious: compare the activity with the customer profile and name the indicator.",
            "How: describe how the money moved, from where to where.")),
        Section("3", "Rules for writing", _p(
            "State facts. Do not state opinions about guilt.",
            "Cite the transaction ID and the amount for every fact. Add up the totals and check the sum.",
            "Use plain words. Write short sentences. Do not use jargon the reader would need to look up.",
            "Leave out anything you cannot check in the records.")),
        Section("4", "Timing", _p(
            f"File {f['suspicious_time']}. See {f['suspicious_time_cite']}.")),
        Section("5", "A short model", _p(
            "Between 3 March and 6 March, the customer received six e-transfers from six different senders (T-1 to T-6, total 17,694). "
            "Within two days, the customer sent 16,900 to three new recipients (T-7 to T-9). "
            "The customer is a student whose profile expects about 1,400 a month. The pattern of many senders to one account, followed by quick "
            "onward transfers, is the indicator for this report.",
            "The IDs in this model are made up. Use the IDs from your own case.")),
    ]
    return Doc(f"guide-{j}", f"{INSTITUTION} Guide to Writing the {f['report_short']} Narrative ({JURISDICTIONS[j]})", "guide", j, secs)


def policy_documents(jurisdiction: str) -> List[Doc]:
    out: List[Doc] = []
    for j in (["ca", "us"] if jurisdiction == "both" else [jurisdiction]):
        out += [policy_manual(j), alert_procedure(j), report_guide(j)]
    return out


# ---- case files --------------------------------------------------------------------------------------------------------------------

def case_documents(t: Tables, jurisdiction: str, window_days: int = 30, max_rows: int = 60) -> List[Doc]:
    """One file per alert. No label, pattern or reason: those live only in ground_truth."""
    j = "us" if jurisdiction == "us" else "ca"
    acct = {a["id"]: a for a in t["accounts"]}
    cust = {c["id"]: c for c in t["customers"]}
    kyc = {k["customer_id"]: k["profile"] for k in t["kyc_profiles"]}
    key_ids = {g["alert_id"]: set(filter(None, g["key_transactions"].split(";"))) for g in t["ground_truth"]}
    involved = {g["alert_id"]: set(filter(None, g["involved_accounts"].split(";"))) for g in t["ground_truth"]}
    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for x in t["transactions"]:
        for side in ("from_account", "to_account"):
            by_account.setdefault(x[side], []).append(x)     # shared outside parties (X0xxxx) are skipped below: they touch everyone
    tx_by_id = {x["id"]: x for x in t["transactions"]}
    alerts_by_account: Dict[str, List[Dict[str, Any]]] = {}
    for a in t["alerts"]:
        alerts_by_account.setdefault(a["account"], []).append(a)
    docs = []
    for a in t["alerts"]:
        c = cust[acct[a["account"]]["customer_id"]]
        end = date.fromisoformat(a["date"]) + timedelta(days=1)
        start = end - timedelta(days=window_days + 1)
        group = {a["account"]} | involved[a["id"]]
        counterparties = {x for k in key_ids[a["id"]] for x in (tx_by_id[k]["from_account"], tx_by_id[k]["to_account"])} - {a["account"]}
        seen, rows = set(), []
        for acc in sorted(group):
            if acc.startswith("X") and not acc.startswith("X9"):
                continue
            for x in by_account.get(acc, []):
                d = date.fromisoformat(x["timestamp"][:10])
                in_window = start <= d <= end
                related = a["account"] in (x["from_account"], x["to_account"]) or (x["from_account"] in group and x["to_account"] in group)
                other = x["to_account"] if x["from_account"] == a["account"] else x["from_account"]
                history = a["account"] in (x["from_account"], x["to_account"]) and other in counterparties      # earlier dealings with the same parties
                if ((in_window and related) or history or x["id"] in key_ids[a["id"]]) and x["id"] not in seen:
                    seen.add(x["id"])
                    rows.append(x)
        rows.sort(key=lambda x: (x["timestamp"], x["id"]))
        trimmed = len(rows) > max_rows
        rows = [r for r in rows if r["id"] in key_ids[a["id"]]] + [r for r in rows if r["id"] not in key_ids[a["id"]]]
        rows = sorted(rows[:max_rows], key=lambda x: (x["timestamp"], x["id"]))
        table = ["Date | ID | From | To | Amount | Channel | Description"] + [
            f"{x['timestamp'].replace('T', ' ')} | {x['id']} | {x['from_account']} | {x['to_account']} | {x['amount']} {x['currency']} | {x['channel']} | {x['description']}"
            for x in rows]
        other = [o["id"] for o in alerts_by_account.get(a["account"], []) if o["id"] != a["id"]]
        secs = [
            Section("summary", "Summary", _p(
                f"Alert {a['id']} fired on {a['date']} for rule {a['rule']}: {RULES[a['rule']]}.",
                f"Account {a['account']} belongs to {c['name']} ({c['id']}). Alert amount: {a['amount']}. Risk score: {a['risk_score']}.",
                f"Other alerts on this account: {', '.join(other) if other else 'none'}.")),
            Section("kyc", "Customer profile", _p(kyc[c["id"]], f"Type: {c['type']}. Occupation or industry: {c['occupation']}. Country: {c['country']}.")),
            Section("transactions", "Transaction extract", _p(
                f"Transfers on the alert account and the accounts it dealt with, from {start.isoformat()} to {end.isoformat()}, "
                f"plus any earlier transfers with the same counterparties"
                + (f" (showing {len(rows)} rows)." if trimmed else "."), *table)),
            Section("note", "Investigator note", _p("Decision (escalate, monitor or dismiss): ______", "Reason, with transaction IDs: ______", "Policy section: ______")),
        ]
        docs.append(Doc(f"case-{a['id']}", f"Case file {a['id']}", "case", j, secs, {"alert_id": a["id"], "account": a["account"], "customer_id": c["id"]}))
    return docs


# ---- rendering ---------------------------------------------------------------------------------------------------------------------

def to_markdown(doc: Doc) -> str:
    out = [f"# {doc.title}", ""]
    for s in doc.sections:
        out += [f"## {s.id}. {s.heading}" if doc.kind != "case" else f"## {s.heading}", ""]
        for para in s.paragraphs:
            out.append(para)
            if para.startswith("Date | ID"):
                out.append(" | ".join("---" for _ in para.split(" | ")))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_chunks(doc: Doc, institution: str = INSTITUTION) -> List[Dict[str, Any]]:
    """Policies and guides: one chunk group per section. Case files: the summary and the profile only. The transaction extract stays a table."""
    chunks: List[Dict[str, Any]] = []
    for s in doc.sections:
        if doc.kind == "case" and s.id in ("transactions", "note"):
            continue
        meta = {"doc_type": doc.kind, "jurisdiction": doc.jurisdiction, "doc_id": doc.id, "section": s.id, "heading": s.heading,
                "institution": institution, "source": "synthetic", **doc.meta}
        chunks += make_chunks(f"{doc.id}-{s.id}", f"{doc.title}: {s.heading}", "\n".join(s.paragraphs), meta)
    return chunks


def doc_text(doc: Doc) -> str:
    return "\n".join([doc.title] + [s.heading + "\n" + "\n".join(s.paragraphs) for s in doc.sections])


CITE_ANY = re.compile(r"(31 CFR \d{4}\.\d+|PCMLTFA s\. [\d.]+|PCMLTF Regulations s\. [\d.]+|PCMLTF STR Regulations s\. [\d.]+)")
