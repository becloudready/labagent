"""The AML table schema. Every generator (builtin, AMLSim, snapshot, importers) produces exactly these tables and columns."""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

Rows = List[Dict[str, Any]]
Tables = Dict[str, Rows]

INSTITUTION = "Maple Ridge Credit Union"

TABLES: Dict[str, List[str]] = {
    "customers": ["id", "name", "type", "occupation", "country", "risk_rating", "expected_monthly_activity", "onboarding_date"],
    "accounts": ["id", "customer_id", "type", "open_date", "branch"],
    "transactions": ["id", "timestamp", "from_account", "to_account", "amount", "currency", "channel", "description"],
    "alerts": ["id", "rule", "account", "date", "amount", "risk_score"],
    "ground_truth": ["alert_id", "label", "pattern", "involved_accounts", "key_transactions", "reason"],
    "kyc_profiles": ["customer_id", "profile"],
    "sanctions_hits": ["id", "customer_id", "customer_name", "list", "sdn_uid", "sdn_name", "match_type", "score", "list_date", "note"],
}

CHANNELS = ("wire", "cash", "e-transfer", "card", "crypto off-ramp")
LABELS = ("escalate", "monitor", "dismiss")
CUSTOMER_TYPES = ("person", "business")
RISK_RATINGS = ("low", "medium", "high")

# The 8 patterns from Altman et al. (2023), arXiv 2306.16424, Figure 2.
PATTERNS = ("fan_out", "fan_in", "gather_scatter", "scatter_gather", "cycle", "random", "bipartite", "stack")
# Laundering that looks like ordinary payments, and a name match. Escalate cases only.
DISGUISED = ("disguised_payroll", "disguised_supplies")
OTHER_TYPOLOGIES = ("sanctions_match",)
# Monitor cases carry the look-alike shape with a "possible_" prefix. Dismiss cases carry "none".
LOOKALIKES = ("possible_fan_in", "possible_cash_structuring", "possible_cycle", "possible_rapid_in_out")
ALL_PATTERNS = PATTERNS + DISGUISED + OTHER_TYPOLOGIES + LOOKALIKES + ("none",)

RULES = {
    "R01": "Cash deposits just under the reporting threshold",
    "R02": "Rapid movement of funds in and out",
    "R03": "Many senders to one account",
    "R04": "One account sends to many recipients",
    "R05": "Funds return to the sender through a loop",
    "R06": "Activity far above expected",
    "R07": "Wire to or from a high-risk country",
    "R08": "Round or just-under-threshold payments to new payees",
    "R09": "Sanctions name match",
}

EXTERNAL_PREFIX = "X"          # accounts at other institutions: they appear in transactions but not in `accounts`
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LIST_SEP = ";"


def write_csv(tables: Tables, out_dir: Path, only: Optional[List[str]] = None) -> List[Path]:
    """One CSV per table (or just the `only` tables). Fixed column order, LF line ends, UTF-8: the same input always gives the same bytes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, cols in TABLES.items():
        if only is not None and name not in only:
            continue
        path = out_dir / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            for row in tables[name]:
                w.writerow({c: row[c] for c in cols})
        paths.append(path)
    return paths


def write_parquet(tables: Tables, out_dir: Path) -> List[Path]:
    """Optional: needs pyarrow (`pip install 'labagent[aml]'`). Raises ImportError if it is missing."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, cols in TABLES.items():
        path = out_dir / f"{name}.parquet"
        pq.write_table(pa.table({c: [str(r[c]) if not isinstance(r[c], (int, float)) else r[c] for r in tables[name]] for c in cols}), path)
        paths.append(path)
    return paths


def read_csv(in_dir: Path, only: Optional[List[str]] = None) -> Tables:
    out: Tables = {}
    for name in TABLES:
        if only is not None and name not in only:
            continue
        with open(in_dir / f"{name}.csv", newline="", encoding="utf-8") as f:
            out[name] = list(csv.DictReader(f))
    return out


def validate(tables: Tables) -> List[str]:
    """Return a list of problems (empty means the dataset is valid). Works on rows from a generator or from read_csv."""
    problems: List[str] = []

    def bad(msg: str) -> None:
        if len(problems) < 50:
            problems.append(msg)

    for name, cols in TABLES.items():
        if name not in tables:
            bad(f"missing table {name}")
            continue
        for i, row in enumerate(tables[name]):
            if list(row.keys()) != cols:
                bad(f"{name} row {i}: columns differ from the schema")
                break
    if problems:
        return problems

    ids = lambda t, k="id": [str(r[k]) for r in tables[t]]
    for t in ("customers", "accounts", "transactions", "alerts", "sanctions_hits"):
        if len(set(ids(t))) != len(tables[t]):
            bad(f"{t}: duplicate ids")
    cust, acct = set(ids("customers")), set(ids("accounts"))
    tx_ids = set(ids("transactions"))
    alert_ids = set(ids("alerts"))

    for r in tables["customers"]:
        if r["type"] not in CUSTOMER_TYPES:
            bad(f"customer {r['id']}: bad type {r['type']}")
        if r["risk_rating"] not in RISK_RATINGS:
            bad(f"customer {r['id']}: bad risk rating")
        if not DATE_RE.match(str(r["onboarding_date"])):
            bad(f"customer {r['id']}: bad onboarding date")
    for r in tables["accounts"]:
        if str(r["customer_id"]) not in cust:
            bad(f"account {r['id']}: unknown customer")
    for r in tables["transactions"]:
        if not TS_RE.match(str(r["timestamp"])):
            bad(f"transaction {r['id']}: bad timestamp")
        if r["channel"] not in CHANNELS:
            bad(f"transaction {r['id']}: bad channel {r['channel']}")
        if float(r["amount"]) <= 0:
            bad(f"transaction {r['id']}: amount must be positive")
        for side in ("from_account", "to_account"):
            a = str(r[side])
            if a not in acct and not a.startswith(EXTERNAL_PREFIX):
                bad(f"transaction {r['id']}: unknown {side} {a}")
        if r["from_account"] == r["to_account"]:
            bad(f"transaction {r['id']}: sends to itself")
    for r in tables["alerts"]:
        if str(r["account"]) not in acct:
            bad(f"alert {r['id']}: unknown account")
        if str(r["rule"]) not in RULES:
            bad(f"alert {r['id']}: unknown rule")
    gt_ids = [str(r["alert_id"]) for r in tables["ground_truth"]]
    if set(gt_ids) != alert_ids or len(gt_ids) != len(alert_ids):
        bad("ground_truth must have exactly one row per alert")
    for r in tables["ground_truth"]:
        if r["label"] not in LABELS:
            bad(f"ground_truth {r['alert_id']}: bad label")
        if r["pattern"] not in ALL_PATTERNS:
            bad(f"ground_truth {r['alert_id']}: unknown pattern {r['pattern']}")
        for t in filter(None, str(r["key_transactions"]).split(LIST_SEP)):
            if t not in tx_ids:
                bad(f"ground_truth {r['alert_id']}: unknown transaction {t}")
        for a in filter(None, str(r["involved_accounts"]).split(LIST_SEP)):
            if a not in acct and not a.startswith(EXTERNAL_PREFIX):
                bad(f"ground_truth {r['alert_id']}: unknown account {a}")
        if not str(r["reason"]).strip():
            bad(f"ground_truth {r['alert_id']}: empty reason")
    kyc = [str(r["customer_id"]) for r in tables["kyc_profiles"]]
    if set(kyc) != cust or len(kyc) != len(cust):
        bad("kyc_profiles must have exactly one row per customer")
    for r in tables["sanctions_hits"]:
        if str(r["customer_id"]) not in cust:
            bad(f"sanctions hit {r['id']}: unknown customer")
        if "TEST DATA" not in str(r["note"]):
            bad(f"sanctions hit {r['id']}: must be tagged TEST DATA")
    return problems


def table_counts(tables: Tables) -> Dict[str, int]:
    return {name: len(tables[name]) for name in TABLES}


def find_pattern_labels(tables: Tables) -> Dict[str, Optional[str]]:
    """Which of the 8 patterns have at least one escalate case. Used by tests and the dataset card."""
    seen = {p for p in PATTERNS}
    have = {r["pattern"] for r in tables["ground_truth"] if r["label"] == "escalate"}
    return {p: p if p in have else None for p in sorted(seen)}
