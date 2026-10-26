"""Which cases go in the folder, and how they are numbered."""
from __future__ import annotations

from typing import Any, Dict, List

from ..schema import Tables

# (pattern, label) in the order cases are picked. The first twelve already hold every kind of exercise the guide needs:
# a graph pattern, a disguised payment, a name match, a look-alike and an ordinary event.
PRIORITY = [("fan_in", "escalate"), ("disguised_payroll", "escalate"), ("sanctions_match", "escalate"), ("possible_fan_in", "monitor"), ("none", "dismiss"),
            ("fan_out", "escalate"), ("cycle", "escalate"), ("possible_cash_structuring", "monitor"), ("gather_scatter", "escalate"),
            ("disguised_supplies", "escalate"), ("stack", "escalate"), ("possible_cycle", "monitor"), ("scatter_gather", "escalate"),
            ("bipartite", "escalate"), ("random", "escalate"), ("possible_rapid_in_out", "monitor"), ("sanctions_match", "monitor")]


def select_cases(t: Tables, n: int) -> List[Dict[str, Any]]:
    """n cases, balanced: one of every kind first (escalate kinds, then look-alikes, then ordinary events), then the rest by alert id.
    Returns [{number, alert, truth}] numbered 1..n in alert-id order, so case numbers follow the dates."""
    truth = {g["alert_id"]: g for g in t["ground_truth"]}
    alerts = {a["id"]: a for a in t["alerts"]}
    chosen: List[str] = []
    for kind, label in PRIORITY:
        hit = next((i for i in sorted(truth) if truth[i]["pattern"] == kind and truth[i]["label"] == label and i not in chosen), None)
        if hit and len(chosen) < n:
            chosen.append(hit)
    for i in sorted(truth):
        if len(chosen) >= n:
            break
        if i not in chosen:
            chosen.append(i)
    chosen = sorted(chosen[:n])
    return [{"number": k, "alert": alerts[i], "truth": truth[i]} for k, i in enumerate(chosen, 1)]
