"""Questions with known answers, for the retrieval check. Each one names the metadata a retrieved chunk must carry to count as a hit."""
from __future__ import annotations

from typing import Any, Dict, List

POLICY = [
    ("Who owns the AML program and who do they report to?", "policy", "2"),
    ("What must staff do before the first transaction for a new customer?", "policy", "3"),
    ("How often is a customer risk rating reviewed?", "policy", "4"),
    ("Which rules raise a transaction monitoring alert?", "policy", "5"),
    ("When must a cash transaction report be filed?", "policy", "6"),
    ("What happens when a customer name is a close match to a sanctions list entry?", "policy", "7"),
    ("When should an investigator escalate instead of monitor?", "policy", "8"),
    ("How long must records be kept?", "policy", "9"),
    ("Can staff tell a customer that a report was filed?", "policy", "10"),
    ("How soon must an investigator open a new alert?", "procedure", "2"),
    ("What are the six questions a report narrative must answer?", "guide", "2"),
]

REGULATIONS = {
    "ca": [("Which section of the Act requires reporting a transaction when there are reasonable grounds to suspect money laundering?", "PCMLTFA s. 7"),
           ("When must a suspicious transaction report be sent to the Centre?", "PCMLTF STR Regulations s. 9"),
           ("What must a financial entity keep when it receives $10,000 or more in cash?", "PCMLTF Regulations s. 10")],
    "us": [("When must a bank file a suspicious activity report?", "31 CFR 1020.320"),
           ("What is the currency transaction reporting requirement for a financial institution?", "31 CFR 1010.311"),
           ("Which rule forbids structuring transactions to evade reporting?", "31 CFR 1010.314")],
}


def policy_qa(jurisdiction: str, with_regulations: bool) -> List[Dict[str, Any]]:
    """Cases in the shape RAGLabClient.evaluate() takes: {question, expect}. With jurisdiction 'both' every question is asked per country."""
    out: List[Dict[str, Any]] = []
    for j in (["ca", "us"] if jurisdiction == "both" else [jurisdiction]):
        out += [{"question": q, "expect": {"doc_type": kind, "section": sec, "jurisdiction": j}} for q, kind, sec in POLICY]
        if with_regulations:
            out += [{"question": q, "expect": {"doc_type": "regulation", "citation": cite, "jurisdiction": j}} for q, cite in REGULATIONS[j]]
    return out
