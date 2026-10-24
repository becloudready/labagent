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

POOL = re.compile(r"^X0\d{4}$")      # ordinary outside parties shared by every customer: following them would pull in the whole book


class CaseIndex:
    """What an investigator can pull for an alert without knowing the answer: no ground truth is read here.

    The alert account's own transfers in the window, the transfers between accounts that deal with it (up to `depth` steps out,
    ignoring the shared outside parties), and the last few earlier transfers with each of its recent counterparties.
    """

    def __init__(self, t: Tables, window_days: int = 30, depth: int = 8, history_per_party: int = 3, max_rows: int = 80):
        self.t, self.window_days, self.depth, self.hist, self.max_rows = t, window_days, depth, history_per_party, max_rows
        self.acct = {a["id"]: a for a in t["accounts"]}
        self.cust = {c["id"]: c for c in t["customers"]}
        self.kyc = {k["customer_id"]: k["profile"] for k in t["kyc_profiles"]}
        self.by_account: Dict[str, List[Dict[str, Any]]] = {}
        for x in t["transactions"]:
            for side in ("from_account", "to_account"):
                self.by_account.setdefault(x[side], []).append(x)
        self.alerts_by_account: Dict[str, List[Dict[str, Any]]] = {}
        for a in t["alerts"]:
            self.alerts_by_account.setdefault(a["account"], []).append(a)
        self._degree: Dict[str, int] = {}

    def uncommon(self, party: str) -> bool:
        """True for another customer of the credit union, or an outside party that few accounts deal with. Ordinary payees (employers,
        shops, landlords) are dealt with by many accounts. Transfers between customers do not happen in ordinary activity."""
        if party in self.acct:
            return True
        if not self._degree:
            seen: Dict[str, set] = {}
            for x in self.t["transactions"]:
                for me, other in ((x["from_account"], x["to_account"]), (x["to_account"], x["from_account"])):
                    if me not in self.acct:
                        seen.setdefault(me, set()).add(other)
            self._degree = {k: len(v) for k, v in seen.items()}
        return self._degree.get(party, 0) < 8

    def customer(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        return self.cust[self.acct[alert["account"]]["customer_id"]]

    def window(self, alert: Dict[str, Any]):
        end = date.fromisoformat(alert["date"]) + timedelta(days=1)
        return end - timedelta(days=self.window_days + 1), end

    def rows(self, alert: Dict[str, Any]):
        """Returns (rows sorted by time, trimmed). Own transfers first when the list has to be cut."""
        start, end = self.window(alert)
        in_win = lambda x: start <= date.fromisoformat(x["timestamp"][:10]) <= end
        own = [x for x in self.by_account.get(alert["account"], []) if in_win(x)]
        seen = {x["id"] for x in own}
        rows = list(own)
        frontier, visited = {alert["account"]}, {alert["account"]}
        for _ in range(self.depth):
            nxt = set()
            for acc in frontier:
                for x in self.by_account.get(acc, []):
                    if not in_win(x):
                        continue
                    other = x["to_account"] if x["from_account"] == acc else x["from_account"]
                    if POOL.match(other) or POOL.match(acc):
                        continue                      # a shared outside party: only the alert account's own transfers (above) include these
                    if x["id"] not in seen:
                        seen.add(x["id"])
                        rows.append(x)
                    if other not in visited:
                        visited.add(other)
                        nxt.add(other)
            frontier = nxt
        parties = {x["to_account"] if x["from_account"] == alert["account"] else x["from_account"] for x in own}
        for party in sorted(parties):
            earlier = [x for x in self.by_account.get(party, []) if alert["account"] in (x["from_account"], x["to_account"])
                       and x["id"] not in seen and x["timestamp"][:10] < start.isoformat()]
            for x in sorted(earlier, key=lambda x: x["timestamp"])[-self.hist:]:
                seen.add(x["id"])
                rows.append(x)
        trimmed = len(rows) > self.max_rows
        if trimmed:
            keep = {x["id"] for x in sorted(rows, key=lambda x: abs((date.fromisoformat(x["timestamp"][:10]) - end).days))[: self.max_rows]}
            rows = [x for x in rows if x["id"] in keep]
        return sorted(rows, key=lambda x: (x["timestamp"], x["id"])), trimmed


def table_lines(rows: List[Dict[str, Any]]) -> List[str]:
    return ["Date | ID | From | To | Amount | Channel | Description"] + [
        f"{x['timestamp'].replace('T', ' ')} | {x['id']} | {x['from_account']} | {x['to_account']} | {x['amount']} {x['currency']} | {x['channel']} | {x['description']}"
        for x in rows]


def case_documents(t: Tables, jurisdiction: str, index: Optional[CaseIndex] = None) -> List[Doc]:
    """One file per alert. No label, pattern or reason: those live only in ground_truth, and nothing here reads them."""
    j = "us" if jurisdiction == "us" else "ca"
    ix = index or CaseIndex(t)
    docs = []
    for a in t["alerts"]:
        c = ix.customer(a)
        start, end = ix.window(a)
        rows, trimmed = ix.rows(a)
        other = [o["id"] for o in ix.alerts_by_account.get(a["account"], []) if o["id"] != a["id"]]
        secs = [
            Section("summary", "Summary", _p(
                f"Alert {a['id']} fired on {a['date']} for rule {a['rule']}: {RULES[a['rule']]}.",
                f"Account {a['account']} belongs to {c['name']} ({c['id']}). Alert amount: {a['amount']}. Risk score: {a['risk_score']}.",
                f"Other alerts on this account: {', '.join(other) if other else 'none'}.")),
            Section("kyc", "Customer profile", _p(ix.kyc[c["id"]], f"Type: {c['type']}. Occupation or industry: {c['occupation']}. Country: {c['country']}.")),
            Section("transactions", "Transaction extract", _p(
                f"Transfers on the alert account and the accounts around it, from {start.isoformat()} to {end.isoformat()}, "
                f"plus the last few earlier transfers with the same counterparties" + (f" (showing the {len(rows)} closest to the alert date)." if trimmed else "."),
                *table_lines(rows))),
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
