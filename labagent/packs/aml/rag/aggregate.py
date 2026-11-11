"""Questions about a whole table (largest, smallest, how many, more than one) cannot be answered from a few retrieved chunks.

`answer_from_table` recognises the common shapes and computes the answer from every row. `needs_full_table` flags the rest, so the
caller can say plainly that its answer rests on a handful of records (issue #40).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional

_AGG = re.compile(r"\b(largest|biggest|highest|greatest|most|maximum|max|smallest|lowest|least|minimum|min|total|sum|average|mean|"
                  r"how many|count|number of|more than|at least|several|multiple|top \d+|every|all)\b", re.I)
_BIG = re.compile(r"\b(largest|biggest|highest|greatest|maximum|max)\b", re.I)
_SMALL = re.compile(r"\b(smallest|lowest|minimum|min)\b", re.I)


def needs_full_table(question: str) -> bool:
    return bool(_AGG.search(question))


def _amount(row: Dict[str, Any]) -> float:
    try:
        return float(str(row.get("amount", "")).replace(",", ""))
    except ValueError:
        return float("nan")


def answer_from_table(question: str, alerts: List[Dict[str, Any]]) -> Optional[str]:
    """A full-table answer for the shapes we can compute, or None."""
    q = question.lower()
    if not alerts or "alert" not in q:
        return None
    n = len(alerts)
    rows = [a for a in alerts if _amount(a) == _amount(a)]
    if "amount" in q and rows and (_BIG.search(q) or _SMALL.search(q)) and not re.search(r"\baccounts?\b.*\bmore than\b", q):
        pick = (max if _BIG.search(q) else min)(rows, key=_amount)
        kind = "largest" if _BIG.search(q) else "smallest"
        return (f"The {kind} alert is {pick['id']}, amount {_amount(pick):.2f}, on account {pick['account']} "
                f"(computed from all {len(rows)} alerts in alerts.csv).")
    words = {"one": 1, "two": 2, "three": 3}
    more = re.search(r"more than (one|two|three|\d+)", q)
    least = re.search(r"at least (one|two|three|\d+)", q)
    if "account" in q and (more or least or re.search(r"multiple|several", q)):
        if more:
            floor = (int(more.group(1)) if more.group(1).isdigit() else words[more.group(1)]) + 1
        elif least:
            floor = int(least.group(1)) if least.group(1).isdigit() else words[least.group(1)]
        else:
            floor = 2
        counts = Counter(a["account"] for a in alerts)
        hit = sorted(((acc, c) for acc, c in counts.items() if c >= floor), key=lambda x: (-x[1], x[0]))
        if not hit:
            return f"No account has {floor} or more alerts (computed from all {n} alerts)."
        shown = ", ".join(f"{acc} ({c})" for acc, c in hit[:50])
        more = f" and {len(hit) - 50} more" if len(hit) > 50 else ""
        return f"{len(hit)} accounts have {floor} or more alerts: {shown}{more} (computed from all {n} alerts)."
    if re.search(r"how many|number of|count", q) and "alert" in q and not re.search(r"\baccount|rule|per\b", q):
        return f"There are {n} alerts in total (counted from alerts.csv)."
    return None


CAVEAT = "\n\n[Not a full check: this answer rests on the {k} retrieved records only. For largest, smallest, count and list questions use lab.tables['alerts'] directly.]"
