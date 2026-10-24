"""The evidence bundle for one alert: policy, customer profile, alert trigger, transactions and similar earlier cases.

Design follows the triage paper (arXiv 2604.19755): everything a reviewer needs to decide, with a source for each part. Nothing here reads
the label, pattern or reason of the alert itself. "Similar cases" are earlier alerts with the same rule: their decisions were already made.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..corpus.documents import CaseIndex
from ..schema import RULES, Tables

# retrieve(question, k, where) -> [{"chunk_id", "text", "metadata"}]
Retrieve = Callable[[str, int, Optional[Dict[str, Any]]], List[Dict[str, Any]]]


def client_retriever(client: Any) -> Retrieve:
    """Adapt RAGLabClient.retrieve() to the shape the bundle uses."""
    def retrieve(question: str, k: int = 3, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return [{"chunk_id": c.chunk_id, "text": c.text, "metadata": c.metadata or {}} for c in client.retrieve(question, top_k=k, where=where)]
    return retrieve


def jurisdiction_of(ix: CaseIndex, alert: Dict[str, Any], jurisdiction: str) -> str:
    if jurisdiction != "both":
        return jurisdiction
    cur = next((x["currency"] for x in ix.by_account.get(alert["account"], [])
                if x["from_account"] == alert["account"] or x["to_account"] == alert["account"]), "CAD")
    return "us" if cur == "USD" else "ca"


def evidence_bundle(t: Tables, alert_id: str, retrieve: Retrieve, jurisdiction: str, index: Optional[CaseIndex] = None, k: int = 3,
                    with_regulations: bool = True) -> Dict[str, Any]:
    ix = index or CaseIndex(t)
    alert = next((a for a in t["alerts"] if a["id"] == alert_id), None)
    if alert is None:
        raise KeyError(f"no alert '{alert_id}'")
    j = jurisdiction_of(ix, alert, jurisdiction)
    cust = ix.customer(alert)
    rows, trimmed = ix.rows(alert)
    rule_text = RULES[alert["rule"]]
    policy = retrieve(f"{rule_text}: what does the policy say?", k, {"doc_type": "policy", "jurisdiction": j})
    regs = retrieve(f"{rule_text} reporting requirement", k, {"doc_type": "regulation", "jurisdiction": j}) if with_regulations else []
    truth = {g["alert_id"]: g for g in t.get("ground_truth", [])}
    earlier = sorted((a for a in t["alerts"] if a["rule"] == alert["rule"] and a["date"] < alert["date"]), key=lambda a: a["date"])[-3:]
    return {
        "alert": alert, "rule": rule_text, "customer": cust, "kyc": ix.kyc[cust["id"]], "jurisdiction": j,
        "sanctions": [h for h in t["sanctions_hits"] if h["customer_id"] == cust["id"]],
        "transactions": rows, "trimmed": trimmed,
        "uncommon_parties": sorted({p for x in rows for p in (x["from_account"], x["to_account"]) if ix.uncommon(p)}),
        "policy": policy, "regulations": regs,
        "similar_cases": [{"alert_id": a["id"], "date": a["date"], "decision": truth[a["id"]]["label"], "reason": truth[a["id"]]["reason"]} for a in earlier if a["id"] in truth],
    }


def render(b: Dict[str, Any], max_rows: int = 40) -> str:
    """Plain text for a person or a prompt."""
    a, c = b["alert"], b["customer"]
    lines = [f"ALERT {a['id']} on {a['date']}: {b['rule']} (rule {a['rule']}), amount {a['amount']}, score {a['risk_score']}",
             f"CUSTOMER {c['id']}: {b['kyc']}"]
    if b["sanctions"]:
        lines += [f"SANCTIONS SCREENING: {h['match_type']} match to {h['sdn_name']} (score {h['score']})" for h in b["sanctions"]]
    lines.append("POLICY:")
    lines += [f"  [{p['chunk_id']}] {p['text'][:300]}" for p in b["policy"]]
    if b["regulations"]:
        lines.append("REGULATIONS:")
        lines += [f"  [{p['metadata'].get('citation', p['chunk_id'])}] {p['text'][:240]}" for p in b["regulations"]]
    lines.append("TRANSACTIONS:")
    lines += [f"  {x['id']} {x['timestamp']} {x['from_account']} -> {x['to_account']} {x['amount']} {x['currency']} {x['channel']} {x['description']}"
              for x in b["transactions"][-max_rows:]]
    if b["similar_cases"]:
        lines.append("SIMILAR EARLIER CASES:")
        lines += [f"  {s['alert_id']} ({s['date']}): {s['decision']}. {s['reason']}" for s in b["similar_cases"]]
    return "\n".join(lines)
