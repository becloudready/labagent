"""Quality checks for the AML RAG lab. Three checks, reported in the result envelope:

1. Retrieval hit rate on a policy and regulation Q&A set. A hard gate (default 0.6, like the SEC tutorial) when the embedding model
   understands meaning. With the keyword-only fallback embedder it is reported but does not fail the launch.
2. Citation validity: every transaction ID, regulation section and policy section a text cites must exist. A hard gate, run on a model
   narrative built from the evidence bundle, so it proves the corpus supports the citations a good answer needs.
3. Triage agreement: escalate, monitor or dismiss compared with ground_truth on a small, balanced set of alerts. Reported only.
   A deterministic reference rule shows the evidence is enough to decide; a chat model is scored the same way when one is configured.
   The labels come from how the data was built, so this is a check that the lab is solvable, not a benchmark of real-world accuracy.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from ..corpus import regs as R
from ..corpus.documents import CaseIndex
from ..schema import LABELS, Tables
from .bundle import Retrieve, evidence_bundle, render
from .qa import policy_qa

HIT_GATE = 0.6
TX_RE = re.compile(r"\bT\d{7}\b")
POLICY_SECTION_RE = re.compile(r"\b(?:policy(?: manual)?|procedure|guide) (?:section|§) ?(\d+)\b", re.I)
LABEL_RE = re.compile(r"\b(escalate|monitor|dismiss)\b", re.I)
BENIGN = {"REAL ESTATE CLOSING PROCEEDS", "PAYROLL BONUS", "TUITION PAYMENT"}


# ---- 1. retrieval --------------------------------------------------------------------------------------------------------------

def retrieval_check(client: Any, jurisdiction: str, with_regulations: bool, top_k: int = 5) -> Dict[str, Any]:
    cases = policy_qa(jurisdiction, with_regulations)
    res = client.evaluate(cases, top_k=top_k)
    return {"questions": res.cases, "hits": res.hits, "hit_rate": res.hit_rate, "misses": [m["question"] for m in res.misses][:10]}


# ---- 2. citations --------------------------------------------------------------------------------------------------------------

def citation_validity(text: str, t: Tables, index: Optional[Dict[str, Any]] = None, policy_sections: Optional[set] = None) -> Dict[str, Any]:
    """Check every transaction ID, regulation section and 'policy section N' reference in `text`. rate is valid / cited (1.0 when nothing is cited)."""
    index = index or R.load_index()
    tx_ids = {x["id"] for x in t["transactions"]}
    policy_sections = policy_sections if policy_sections is not None else {str(n) for n in range(1, 12)}
    cited_tx = sorted(set(TX_RE.findall(text)))
    bad_tx = [x for x in cited_tx if x not in tx_ids]
    cited_reg = [f"{i} {n}" for i, nums in R.cited_sections(text).items() for n in nums]
    bad_reg = R.missing_citations(text, index)
    cited_pol = sorted(set(POLICY_SECTION_RE.findall(text)))
    bad_pol = [s for s in cited_pol if s not in policy_sections]
    total = len(cited_tx) + len(cited_reg) + len(cited_pol)
    invalid = bad_tx + bad_reg + [f"policy section {s}" for s in bad_pol]
    return {"cited": total, "invalid": invalid, "rate": 1.0 if total == 0 else round((total - len(invalid)) / total, 3),
            "transactions": len(cited_tx), "regulations": len(cited_reg), "policy_sections": len(cited_pol)}


def model_narrative(b: Dict[str, Any]) -> str:
    """A reference narrative built only from the bundle. Every ID and section it cites comes from retrieved or listed evidence."""
    a, c = b["alert"], b["customer"]
    acct = a["account"]
    mine = [x for x in b["transactions"] if acct in (x["from_account"], x["to_account"])]
    top = sorted(mine, key=lambda x: -float(x["amount"]))[:5]
    top.sort(key=lambda x: x["timestamp"])
    ids = ", ".join(x["id"] for x in top)
    total = sum(float(x["amount"]) for x in top)
    section = next((p["metadata"].get("section") for p in b["policy"] if p["metadata"].get("doc_type") == "policy"), None)
    reg = next((p["metadata"].get("citation") for p in b["regulations"]), None)
    out = [f"Alert {a['id']} on account {acct} for {b['rule'].lower()}.",
           f"Between {top[0]['timestamp'][:10]} and {top[-1]['timestamp'][:10]} the account had large transfers ({ids}), about {total:,.0f} in total." if top else
           "The account had no transfers in the review window.",
           f"The profile expects about {int(c['expected_monthly_activity']):,} a month."]
    if section:
        out.append(f"Policy section {section} applies.")
    if reg:
        out.append(f"See {reg}.")
    return " ".join(out)


# ---- 3. triage -----------------------------------------------------------------------------------------------------------------

def _loop_of_three_or_more(acct: str, edges: Dict[str, set], limit: int = 7) -> bool:
    def walk(node: str, depth: int, path: List[str]) -> bool:
        for nxt in sorted(edges.get(node, ())):
            if nxt == acct and depth >= 3:
                return True
            if nxt not in path and depth < limit and walk(nxt, depth + 1, path + [nxt]):
                return True
        return False
    return walk(acct, 1, [acct])


def triage_baseline(b: Dict[str, Any]) -> str:
    """A fixed reference rule that uses only the bundle. It shows the evidence is enough to decide; it is not a model."""
    a, c = b["alert"], b["customer"]
    rule, acct = a["rule"], a["account"]
    if rule == "R09":
        return "escalate" if any(h["match_type"] == "exact" for h in b["sanctions"]) else "monitor"
    ratio = float(a["amount"]) / max(float(c["expected_monthly_activity"]), 1.0)
    unusual = set(b["uncommon_parties"]) - {acct}
    end = date.fromisoformat(a["date"])
    rows = b["transactions"]
    days = lambda x: (end - date.fromisoformat(x["timestamp"][:10])).days
    recent = [x for x in rows if acct in (x["from_account"], x["to_account"]) and 0 <= days(x) <= 21]
    ins = [x for x in recent if x["to_account"] == acct]
    outs = [x for x in recent if x["from_account"] == acct]
    if rule in ("R06", "R07") and {x["description"] for x in recent} & BENIGN:
        return "dismiss"
    if rule == "R08":
        for x in outs:
            same = [y for y in rows if y["to_account"] == x["to_account"] and y["from_account"] == acct and y["amount"] == x["amount"] and y["timestamp"] < x["timestamp"]]
            if len(same) >= 2:
                return "dismiss"              # the same amount went to the same payee before, so the payee is not new
        if any(x["description"].startswith("SUPPLIES INVOICE") and 9000 <= float(x["amount"]) < 10000 for x in outs):
            return "escalate"
        return "monitor"
    if rule == "R03":
        return "escalate" if len({x["from_account"] for x in ins} & unusual) >= 3 and ratio >= 1 else "monitor"
    if rule == "R04":
        return "escalate" if len({x["to_account"] for x in outs} & unusual) >= 3 and ratio >= 1 else "monitor"
    if rule == "R05":
        edges: Dict[str, set] = {}
        for x in rows:
            if x["from_account"] in unusual | {acct} and x["to_account"] in unusual | {acct}:
                edges.setdefault(x["from_account"], set()).add(x["to_account"])
        return "escalate" if ratio >= 1 and _loop_of_three_or_more(acct, edges) else "monitor"
    if rule == "R06":
        return "escalate" if len({x["from_account"] for x in ins} & unusual) >= 3 else "monitor"
    if rule == "R02":
        legs = [x for x in rows if x["from_account"] in unusual | {acct} and x["to_account"] in unusual | {acct}]
        return "escalate" if len(legs) >= 3 and ratio >= 1 else "monitor"
    return "monitor"


def triage_llm(client: Any, b: Dict[str, Any]) -> Optional[str]:
    prompt = ("You review an anti-money laundering alert. Read the evidence and answer with one word: escalate, monitor or dismiss. "
              "Then give the transaction IDs and policy section you relied on.")
    res = client.llm.complete(prompt, render(b))
    m = LABEL_RE.search(res.text or "")
    return m.group(1).lower() if m else None


def sample_alerts(t: Tables, per_label: int = 4) -> List[str]:
    """A small balanced set: the first `per_label` alerts of each label, by alert id."""
    chosen: Dict[str, List[str]] = {l: [] for l in LABELS}
    for g in sorted(t["ground_truth"], key=lambda g: g["alert_id"]):
        if len(chosen[g["label"]]) < per_label:
            chosen[g["label"]].append(g["alert_id"])
    return sorted(i for ids in chosen.values() for i in ids)


def triage_check(t: Tables, retrieve: Retrieve, jurisdiction: str, with_regulations: bool, client: Any = None, per_label: int = 4,
                 index: Optional[CaseIndex] = None) -> Dict[str, Any]:
    ix = index or CaseIndex(t)
    truth = {g["alert_id"]: g["label"] for g in t["ground_truth"]}
    ids = sample_alerts(t, per_label)
    base_ok = llm_ok = llm_n = 0
    wrong: List[str] = []
    for aid in ids:
        b = evidence_bundle(t, aid, retrieve, jurisdiction, ix, with_regulations=with_regulations)
        if triage_baseline(b) == truth[aid]:
            base_ok += 1
        else:
            wrong.append(aid)
        if client is not None:
            guess = triage_llm(client, b)
            llm_n += 1
            llm_ok += guess == truth[aid]
    out = {"alerts": len(ids), "reference_rule": {"agreement": round(base_ok / len(ids), 3), "disagreed": wrong[:10]},
           "chat_model": None}
    if client is not None and llm_n:
        out["chat_model"] = {"agreement": round(llm_ok / llm_n, 3), "alerts": llm_n}
    return out


# ---- all three -----------------------------------------------------------------------------------------------------------------

def run_quality(client: Any, t: Tables, jurisdiction: str, with_regulations: bool, min_hit_rate: float = HIT_GATE, use_chat_model: bool = False,
                policy_sections: Optional[set] = None) -> Dict[str, Any]:
    """Returns {retrieval, citations, triage, passed, failures}. `passed` covers the two hard gates only."""
    from .bundle import client_retriever
    retrieve = client_retriever(client)
    fallback = str(getattr(client.embedder, "name", "")).startswith("hashing")
    retrieval = retrieval_check(client, jurisdiction, with_regulations)
    retrieval["gate"] = None if fallback else min_hit_rate
    ix = CaseIndex(t)
    narratives = []
    for aid in sample_alerts(t, 3):
        b = evidence_bundle(t, aid, retrieve, jurisdiction, ix, with_regulations=with_regulations)
        narratives.append(citation_validity(model_narrative(b), t, policy_sections=policy_sections))
    cited = sum(n["cited"] for n in narratives)
    invalid = [x for n in narratives for x in n["invalid"]]
    citations = {"narratives": len(narratives), "cited": cited, "invalid": invalid[:10],
                 "rate": 1.0 if not cited else round((cited - len(invalid)) / cited, 3)}
    triage = triage_check(t, retrieve, jurisdiction, with_regulations, client if use_chat_model else None, index=ix)
    failures = []
    if not fallback and retrieval["hit_rate"] < min_hit_rate:
        failures.append(f"retrieval hit rate {retrieval['hit_rate']:.2f} is below the {min_hit_rate} gate")
    if citations["rate"] < 1.0:
        failures.append(f"citation validity {citations['rate']:.2f}: invalid citations {citations['invalid']}")
    return {"retrieval": retrieval, "citations": citations, "triage": triage, "embedding_model": getattr(client.embedder, "name", "unknown"),
            "keyword_only_embedder": fallback, "passed": not failures, "failures": failures}
