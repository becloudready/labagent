"""Pure-Python synthetic AML data. No Java, no network, deterministic for a given seed.

Normal activity (payroll, rent, card spending, business sales and suppliers) is generated first. Then labelled cases are laid
over it: the 8 laundering patterns from Altman et al. (2023), two ways of disguising laundering as ordinary payments, name
matches against a fixed set of OFAC SDN entity names, look-alikes that deserve a closer look (monitor) and ordinary events
that trigger a rule for a harmless reason (dismiss).

Labels are set by construction, not by an investigator. The dataset card says so.

Only these random calls are used: random(), randint(), choice(), gauss(), shuffle(), sample(). They give the same numbers for
the same seed on every supported Python.
"""
from __future__ import annotations

import difflib
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import names as N
from ..schema import EXTERNAL_PREFIX, LIST_SEP, PATTERNS, RULES, TABLES, Tables, validate
from . import Dataset, Generator

SIZES = {  # customers, laundering cases per kind, monitor cases, dismiss cases, sanctions seeds (exact, fuzzy)
    "tiny": dict(customers=60, per_kind=1, monitor=4, dismiss=8, sanctions=(1, 1)),      # for tests
    "small": dict(customers=300, per_kind=2, monitor=16, dismiss=30, sanctions=(2, 4)),
    "medium": dict(customers=2000, per_kind=12, monitor=120, dismiss=300, sanctions=(4, 8)),
}
PROSE = {"fan_out": "fan-out", "fan_in": "fan-in", "gather_scatter": "gather-scatter", "scatter_gather": "scatter-gather", "random": "random-walk"}
THRESHOLD = 10000        # reporting threshold, in the account currency, for cash and wires
DATA = Path(__file__).resolve().parent.parent / "data"
VERSION = "1"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class _Build:
    def __init__(self, seed: int, size: str, jurisdiction: str, base: date):
        self.rng = random.Random(seed)
        self.cfg = SIZES[size]
        self.jur = jurisdiction
        self.base = base
        self.customers: List[Dict[str, Any]] = []
        self.accounts: List[Dict[str, Any]] = []
        self.tx: List[Dict[str, Any]] = []            # drafts: carry "_k" until final ids are assigned in time order
        self.cases: List[Dict[str, Any]] = []
        self.sanctions: List[Dict[str, Any]] = []
        self.kyc: Dict[str, str] = {}
        self.cur: Dict[str, str] = {}                  # account id -> currency
        self.primary: Dict[str, str] = {}              # customer id -> main account id
        self.owner: Dict[str, str] = {}                # account id -> customer id
        self.amt: Dict[int, float] = {}
        self.dayof: Dict[int, int] = {}
        self._k = 0
        self._ext = 0
        self._illicit = 0

    # ---- small helpers -------------------------------------------------------------------------------------------------------
    def when(self, day: float, hour: Optional[int] = None) -> datetime:
        d = self.base + timedelta(days=int(day))
        h = self.rng.randint(8, 18) if hour is None else hour
        return datetime(d.year, d.month, d.day, h, self.rng.randint(0, 59), self.rng.randint(0, 59))

    def ext(self) -> str:
        self._ext += 1
        return f"{EXTERNAL_PREFIX}{(self._ext % 400) + 1:05d}"                 # a shared pool of ordinary outside parties

    def illicit_ext(self) -> str:
        self._illicit += 1
        return f"{EXTERNAL_PREFIX}9{self._illicit:04d}"

    def amount(self, mean: float, spread: float = 0.25) -> float:
        return round(max(5.0, self.rng.gauss(mean, mean * spread)), 2)

    def add(self, ts: datetime, src: str, dst: str, amt: float, channel: str, desc: str, cur: Optional[str] = None) -> int:
        self._k += 1
        self.amt[self._k] = round(amt, 2)
        self.dayof[self._k] = (ts.date() - self.base).days
        self.tx.append({"_k": self._k, "timestamp": _iso(ts), "from_account": src, "to_account": dst, "amount": round(amt, 2),
                        "currency": cur or self.cur.get(src) or self.cur.get(dst, "CAD"), "channel": channel, "description": desc})
        return self._k

    # ---- customers and accounts ----------------------------------------------------------------------------------------------
    def make_customers(self) -> None:
        r = self.rng
        for i in range(1, self.cfg["customers"] + 1):
            cid = f"C{i:05d}"
            if r.random() < 0.15:
                stem, (industry, suffix, rev, cash) = r.choice(N.BUSINESS_STEMS), r.choice(N.BUSINESS_KINDS)
                ctype, name, occ, monthly = "business", f"{stem} {suffix}", industry, rev * r.uniform(0.6, 1.4)
            else:
                (occ, income) = r.choice(N.OCCUPATIONS)
                ctype, name, monthly, cash = "person", f"{r.choice(N.FIRST)} {r.choice(N.LAST)}", income * r.uniform(0.7, 1.3), False
            country = r.choice(["CA", "US"]) if self.jur == "both" else self.jur.upper()
            if r.random() < 0.06:
                country = r.choice(N.FOREIGN)
            risk = "low"
            if ctype == "business" and cash:
                risk = "medium"
            if country in N.HIGH_RISK_COUNTRIES or r.random() < 0.05:
                risk = "high" if r.random() < 0.5 else "medium"
            years = r.randint(0, 9) * 365 + r.randint(30, 360) if r.random() > 0.08 else -r.randint(5, 300)   # a few are new this year
            onboard = self.base - timedelta(days=years)
            self.customers.append({"id": cid, "name": name, "type": ctype, "occupation": occ, "country": country, "risk_rating": risk,
                                   "expected_monthly_activity": int(round(monthly, -2)), "onboarding_date": onboard.isoformat(),
                                   "_cash": cash})
            home = "CAD" if country == "CA" else "USD"
            kinds = ["business chequing"] if ctype == "business" else ["chequing"] + (["savings"] if r.random() < 0.5 else [])
            for k in kinds:
                aid = f"ACC{len(self.accounts) + 1:06d}"
                self.accounts.append({"id": aid, "customer_id": cid, "type": k, "open_date": onboard.isoformat(), "branch": r.choice(N.BRANCHES)})
                self.cur[aid] = home
                self.owner[aid] = cid
                self.primary.setdefault(cid, aid)

    # ---- normal activity -----------------------------------------------------------------------------------------------------
    def normal_activity(self) -> None:
        r = self.rng
        for c in self.customers:
            acct, monthly = self.primary[c["id"]], float(c["expected_monthly_activity"])
            start = max(0, (date.fromisoformat(c["onboarding_date"]) - self.base).days)
            employer = r.choice(N.EMPLOYERS)
            for month in range(12):
                m0 = month * 30
                if m0 + 29 < start:
                    continue
                if c["type"] == "person":
                    for half in (0, 15):
                        self.add(self.when(m0 + half, 9), self.ext(), acct, self.amount(monthly / 2, 0.03), "e-transfer", f"PAYROLL {employer.upper()}")
                    self.add(self.when(m0 + 1, 10), acct, self.ext(), self.amount(monthly * 0.3, 0.02), "e-transfer", "RENT")
                    self.add(self.when(m0 + r.randint(3, 25)), acct, self.ext(), self.amount(monthly * 0.04, 0.4), "card", "UTILITY BILL")
                    for _ in range(r.randint(4, 9)):
                        what = r.choice(["GROCERY", "FUEL", "RESTAURANT", "PHARMACY", "ONLINE SHOP"])
                        self.add(self.when(m0 + r.randint(0, 29)), acct, self.ext(), self.amount(monthly * 0.015, 0.6), "card", f"POS PURCHASE {what}")
                    if r.random() < 0.6:
                        self.add(self.when(m0 + r.randint(0, 29)), acct, self.ext(), self.amount(monthly * 0.03, 0.5), "cash", "ATM WITHDRAWAL")
                    if r.random() < 0.5:
                        self.add(self.when(m0 + r.randint(0, 29)), acct, self.ext(), self.amount(monthly * 0.02, 0.6), "e-transfer", "E-TRANSFER SENT")
                else:
                    for _ in range(r.randint(6, 12)):
                        self.add(self.when(m0 + r.randint(0, 29)), self.ext(), acct, self.amount(monthly / 9, 0.5), "card", "CARD SETTLEMENT")
                    if c["_cash"]:
                        for _ in range(r.randint(2, 4)):
                            self.add(self.when(m0 + r.randint(0, 29)), self.ext(), acct, self.amount(min(monthly / 12, 6000), 0.4), "cash", "BRANCH CASH DEPOSIT")
                    for _ in range(r.randint(2, 5)):
                        self.add(self.when(m0 + r.randint(0, 29)), acct, self.ext(), self.amount(monthly / 7, 0.5), "wire" if r.random() < 0.3 else "e-transfer",
                                 f"SUPPLIER INVOICE {r.randint(1000, 9999)}")
                    self.add(self.when(m0 + 27, 11), acct, self.ext(), self.amount(monthly * 0.25, 0.05), "e-transfer", "PAYROLL RUN")

    # ---- cases ---------------------------------------------------------------------------------------------------------------
    def established(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Customers whose account was open before the data starts, so a case never happens before the account existed."""
        cut = self.base.isoformat()
        return [c for c in self.customers if c["onboarding_date"] < cut and (kind is None or c["type"] == kind)]

    def mule_pool(self) -> List[str]:
        """Accounts whose owners expect little activity, so a large flow does not match their profile."""
        low = sorted(self.customers, key=lambda c: (c["expected_monthly_activity"], c["id"]))
        low = [c for c in low if c["type"] == "person" and c["onboarding_date"] < self.base.isoformat()][: max(12, int(len(self.customers) * 0.4))]
        pool = [self.primary[c["id"]] for c in low]
        self.rng.shuffle(pool)
        return pool

    def take(self, pool: List[str], k: int) -> List[str]:
        out = []
        for _ in range(k):
            out.append(pool.pop(0))
            pool.append(out[-1])          # reuse across cases only after the whole pool has been used
        return out

    def mix(self, pool: List[str], k: int, internal_share: float = 0.6) -> List[str]:
        n_in = max(1, int(round(k * internal_share)))
        accts = self.take(pool, n_in) + [self.illicit_ext() for _ in range(k - n_in)]
        self.rng.shuffle(accts)
        return accts

    def case(self, label: str, pattern: str, rule: str, key_account: str, involved: List[str], keys: List[int], reason: str,
             amount: float, day: float, score: Tuple[int, int]) -> None:
        last = max(self.dayof[k] for k in keys)          # the alert fires on the day of the last transfer it covers
        self.cases.append({"label": label, "pattern": pattern, "rule": rule, "account": key_account, "involved": involved, "keys": keys,
                           "reason": reason, "amount": round(amount, 2), "day": max(int(day), last), "score": self.rng.randint(*score)})

    def expected_of(self, acct: str) -> float:
        c = next(x for x in self.customers if x["id"] == self.owner[acct])
        return float(c["expected_monthly_activity"])

    def launder(self, pattern: str, pool: List[str]) -> None:
        r = self.rng
        total = float(r.randint(18, 80) * 1000)
        t0 = r.randint(20, 300)
        keys: List[int] = []
        ch = lambda: r.choice(["wire", "e-transfer", "e-transfer"])
        legs: List[Tuple[int, str, str, float]] = []        # (day offset, from, to, amount)
        rule, key_account = "R02", ""

        if pattern == "fan_out":
            k = r.randint(4, 8)
            src, hub = self.illicit_ext(), self.take(pool, 1)[0]
            outs = self.mix(pool, k)
            legs.append((0, src, hub, total))
            legs += [(1 + (i % 2), hub, o, total / k * r.uniform(0.9, 0.99)) for i, o in enumerate(outs)]
            rule, key_account = "R04", hub
        elif pattern == "fan_in":
            k = r.randint(4, 8)
            ins, hub = self.mix(pool, k), self.take(pool, 1)[0]
            legs += [(i % 3, s, hub, total / k * r.uniform(0.9, 1.05)) for i, s in enumerate(ins)]
            rule, key_account = "R03", hub
        elif pattern == "gather_scatter":
            k = r.randint(3, 5)
            ins, hub, outs = self.mix(pool, k), self.take(pool, 1)[0], self.mix(pool, k)
            legs += [(i % 2, s, hub, total / k) for i, s in enumerate(ins)]
            legs += [(3 + i % 2, hub, o, total / k * 0.97) for i, o in enumerate(outs)]
            rule, key_account = "R02", hub
        elif pattern == "scatter_gather":
            k = r.randint(3, 5)
            origin, mids, sink = self.take(pool, 1)[0], self.mix(pool, k), self.illicit_ext()
            legs += [(i % 2, origin, m, total / k) for i, m in enumerate(mids)]
            legs += [(2 + i % 2, m, sink, total / k * 0.97) for i, m in enumerate(mids)]
            rule, key_account = "R02", origin
        elif pattern == "cycle":
            ring = self.take(pool, r.randint(3, 5))
            amt = total
            for i, a in enumerate(ring):
                b = ring[(i + 1) % len(ring)]
                legs.append((i, a, b, amt))
                amt *= r.uniform(0.97, 0.995)
            rule, key_account = "R05", ring[0]
        elif pattern == "random":
            walk = self.mix(pool, r.randint(5, 8), 0.7)
            amt = total
            for i in range(len(walk) - 1):
                legs.append((i, walk[i], walk[i + 1], amt))
                amt *= r.uniform(0.95, 0.99)
            rule = "R02"
            key_account = next((a for a in walk if a in self.owner), self.take(pool, 1)[0])
        elif pattern == "bipartite":
            ins, outs = self.mix(pool, r.randint(2, 4), 0.7), self.mix(pool, r.randint(2, 4), 0.5)
            legs += [(j % 3, s, o, total / (len(ins) * len(outs))) for j, (s, o) in enumerate((s, o) for s in ins for o in outs)]
            rule = "R02"
            key_account = next((a for a in ins + outs if a in self.owner), self.take(pool, 1)[0])
        elif pattern == "stack":
            l1, l2, l3 = self.mix(pool, 2, 0.7), self.mix(pool, 3, 0.6), self.mix(pool, 3, 0.4)
            legs += [(j % 2, s, o, total / 6) for j, (s, o) in enumerate((s, o) for s in l1 for o in l2)]
            legs += [(3 + j % 2, s, o, total / 9 * 0.97) for j, (s, o) in enumerate((s, o) for s in l2 for o in l3)]
            rule = "R02"
            key_account = next((a for a in l1 + l2 + l3 if a in self.owner), self.take(pool, 1)[0])
        else:
            raise ValueError(pattern)

        involved: List[str] = []
        for off, s, d, a in legs:
            if s == d:
                continue
            channel = "cash" if (s.startswith(EXTERNAL_PREFIX) and pattern == "fan_out" and off == 0 and total < 60000) else ch()
            keys.append(self.add(self.when(t0 + off), s, d, a, channel, self.cover_story(channel)))
            for x in (s, d):
                if x not in involved:
                    involved.append(x)
        span = max(o for o, *_ in legs) + 1
        moved = sum(a for *_, a in legs)
        ratio = moved / max(self.expected_of(key_account), 1.0)
        self.case("escalate", pattern, rule, key_account, involved, keys,
                  f"{len(keys)} transfers moved about {int(moved):,} across {len(involved)} accounts in {span} days, in a {PROSE.get(pattern, pattern)} shape. "
                  f"That is {ratio:.1f} times the monthly activity this customer's profile expects, and most counterparties are new.",
                  moved, t0 + span, (72, 96))

    def cover_story(self, channel: str) -> str:
        r = self.rng
        if channel == "cash":
            return "BRANCH CASH DEPOSIT"
        return r.choice(["E-TRANSFER SENT", "PERSONAL TRANSFER", "FAMILY SUPPORT", "WIRE TRANSFER", "INVOICE", "LOAN REPAYMENT"])

    def disguised(self, kind: str, pool: List[str]) -> None:
        """A front business collects 'sales' from mule accounts, then pays them out as payroll or supplies."""
        r = self.rng
        fronts = self.established("business")
        front_c = r.choice(fronts)
        front, t0 = self.primary[front_c["id"]], r.randint(30, 280)
        payers, keys = self.take(pool, r.randint(4, 6) if kind == "disguised_supplies" else r.randint(3, 5)), []
        involved = [front] + payers
        collected = 0.0
        for i, p in enumerate(payers):
            a = round(r.uniform(6, 14) * 1000, 2)
            collected += a
            keys.append(self.add(self.when(t0 + i), p, front, a, "e-transfer", f"INVOICE PAYMENT {r.randint(1000, 9999)}"))
        outs, paid = [], 0.0
        n_out = r.randint(3, 5)
        if kind == "disguised_supplies":
            n_out = max(2, min(n_out, int(collected // 9600)))
        for i in range(n_out):
            if kind == "disguised_payroll":
                dst = self.take(pool, 1)[0] if i < n_out - 1 else self.illicit_ext()
                a, ch, desc = round(collected / n_out * r.uniform(0.95, 1.0), 2), "e-transfer", "PAYROLL"
            else:
                dst = self.illicit_ext()
                a, ch, desc = round(r.uniform(9300, max(9301, min(9950, collected / n_out))), 2), "wire", f"SUPPLIES INVOICE {r.randint(1000, 9999)}"
            outs.append(dst)
            paid += a
            keys.append(self.add(self.when(t0 + 3 + i * r.randint(1, 3)), front, dst, a, ch, desc))
        involved += outs
        rule = "R06" if kind == "disguised_payroll" else "R08"
        why = ("pays 'payroll' to people who are not on its payroll, at no regular schedule, right after receiving invoice payments from individuals"
               if kind == "disguised_payroll" else
               "pays 'supplies' invoices to new payees in amounts just under the 10,000 reporting threshold, funded by invoice payments from individuals")
        self.case("escalate", kind, rule, front, involved, keys,
                  f"The business received {int(collected):,} and sent out {int(paid):,} within about a week. It {why}. "
                  f"The activity is {collected / max(float(front_c['expected_monthly_activity']), 1):.1f} times what the business profile expects.",
                  collected, t0 + 8, (68, 92))

    def lookalike(self, kind: str, pool: List[str]) -> None:
        r = self.rng
        t0, keys = r.randint(20, 320), []
        c = r.choice(self.established("person"))
        acct = self.primary[c["id"]]
        if kind == "possible_fan_in":
            friends = [self.ext() for _ in range(r.randint(4, 6))]
            keys = [self.add(self.when(t0 + i % 2), f, acct, round(r.uniform(40, 150), 2), "e-transfer", "BIRTHDAY GIFT") for i, f in enumerate(friends)]
            rule, involved = "R03", [acct] + friends
            reason = (f"{len(friends)} small e-transfers arrived in two days, which looks like a fan-in. The amounts are small, the notes say gift, and the funds stayed in the account. "
                      "Keep watching: no evidence of onward movement.")
        elif kind == "possible_cash_structuring":
            acct = self.primary[r.choice([x for x in self.established("business") if x["_cash"]] or [c])["id"]]
            keys = [self.add(self.when(t0 + i * 2), self.ext(), acct, round(r.uniform(8200, 9800), 2), "cash", "BRANCH CASH DEPOSIT") for i in range(3)]
            rule, involved = "R01", [acct]
            reason = ("Three cash deposits just under 10,000 in one week. The customer runs a cash business and makes regular cash deposits, "
                      "but these are much larger than usual and sit close to the threshold. Keep watching and ask for the sales records.")
        elif kind == "possible_cycle":
            other = self.mix(pool, 1, 1.0)[0]
            while other == acct:
                other = self.mix(pool, 1, 1.0)[0]
            a = round(r.uniform(900, 2400), 2)
            keys = [self.add(self.when(t0), acct, other, a, "e-transfer", "LOAN TO FRIEND"), self.add(self.when(t0 + 20), other, acct, a, "e-transfer", "LOAN REPAYMENT")]
            rule, involved = "R05", [acct, other]
            reason = "Money went to another customer and came back in full 20 days later. The notes say loan and repayment, and the amounts are small. Keep watching for repeats."
        else:
            supplier = self.ext()
            a = round(r.uniform(4000, 9000), 2)
            keys = [self.add(self.when(t0, 10), self.ext(), acct, a, "wire", "CLIENT PAYMENT"), self.add(self.when(t0, 15), acct, supplier, round(a * 0.97, 2), "wire", "SUPPLIER INVOICE 4471")]
            rule, involved = "R02", [acct, supplier]
            reason = "A payment came in and 97 percent left the same day to a supplier payee. Businesses do pay suppliers by wire, but the invoice is not on file. Keep watching and ask for it."
        self.case("monitor", kind, rule, acct, involved, keys, reason, sum(self.amt[k] for k in keys), t0 + 3, (45, 65))

    def benign(self, kind: int) -> None:
        r = self.rng
        t0, c = r.randint(20, 330), r.choice(self.established())
        acct, keys = self.primary[c["id"]], []
        if kind == 0:
            a = round(r.uniform(60, 240) * 1000, 2)
            keys = [self.add(self.when(t0, 11), self.ext(), acct, a, "wire", "REAL ESTATE CLOSING PROCEEDS")]
            rule, why = "R06", "A single large wire from a law firm for the sale of a home. The customer told the branch about the sale in advance and the deed is on file."
        elif kind == 1:
            a = round(c["expected_monthly_activity"] * r.uniform(2.5, 4.0), 2)
            keys = [self.add(self.when(t0, 9), self.ext(), acct, a, "e-transfer", "PAYROLL BONUS")]
            rule, why = "R06", "A one-time bonus from the customer's regular employer. The sender matches the payroll deposits of the last twelve months."
        elif kind == 2:
            payee, each = self.ext(), round(r.uniform(900, 1100), 2)
            t0 = max(t0, 100)
            for back in (90, 60):         # the payee is not new: the same amount went to it earlier (ordinary activity, not part of the case)
                self.add(self.when(t0 - back), acct, payee, each, "e-transfer", "FAMILY SUPPORT")
            keys = [self.add(self.when(t0 + i * 7), acct, payee, each, "e-transfer", "FAMILY SUPPORT") for i in range(4)]
            a = sum(self.amt[k] for k in keys)
            rule, why = "R08", "Four equal weekly e-transfers with the note family support. The customer paid the same payee the same amount twice in the previous three months, so the payee is not new."
        else:
            a = round(r.uniform(18, 32) * 1000, 2)
            keys = [self.add(self.when(t0, 10), acct, self.ext(), a, "wire", "TUITION PAYMENT")]
            rule, why = "R07", "A wire to a foreign university for tuition. The invoice is on file and the customer is enrolled."
        self.case("dismiss", "none", rule, acct, [acct], keys, why, a, t0 + 1, (15, 35))

    def sanctions_cases(self) -> None:
        entries = json.loads((DATA / "sdn_seed.json").read_text())
        meta = {"list_date": entries["list_date"]}
        n_exact, n_fuzzy = self.cfg["sanctions"]
        biz = self.established("business")
        picks = self.rng.sample(biz, min(len(biz), n_exact + n_fuzzy))
        for i, c in enumerate(picks):
            e = entries["entries"][i % len(entries["entries"])]
            exact = i < n_exact
            name = e["name"] if exact else self.vary(e["name"])
            c["name"], c["occupation"], c["_cash"] = name, "import and export", False
            score = 1.0 if exact else round(difflib.SequenceMatcher(None, name, e["name"]).ratio(), 2)
            self.sanctions.append({"customer_id": c["id"], "customer_name": name, "list": "OFAC SDN (seeded test entry)", "sdn_uid": e["uid"],
                                   "sdn_name": e["name"], "match_type": "exact" if exact else "fuzzy", "score": f"{score:.2f}", "list_date": meta["list_date"],
                                   "note": "TEST DATA: this customer is invented and was named to match a list entry for screening practice"})
            acct = self.primary[c["id"]]
            t0 = self.rng.randint(30, 330)
            keys = [self.add(self.when(t0 + j * 3), acct, self.illicit_ext(), round(self.rng.uniform(12, 40) * 1000, 2), "wire", f"WIRE INVOICE {self.rng.randint(1000, 9999)}") for j in range(2)]
            reason = ("The customer name is identical to a sanctioned entity on the list and the account sends wires abroad. Treat as a true match until cleared."
                      if exact else
                      f"The customer name is close to a listed entity (score {score:.2f}) but not identical. Compare registration details and ownership before deciding.")
            self.case("escalate" if exact else "monitor", "sanctions_match", "R09", acct, [acct], keys, reason,
                      sum(self.amt[k] for k in keys), t0 + 4, (80, 98) if exact else (50, 70))

    def vary(self, name: str) -> str:
        """A near-miss spelling of a list name: two adjacent letters swapped, and sometimes LIMITED shortened."""
        r = self.rng
        for _ in range(8):
            words = name.replace(".", "").split()
            i = r.randint(0, len(words) - 1)
            w = words[i]
            if len(w) > 4:
                j = r.randint(1, len(w) - 3)
                words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2:]
            out = " ".join(words)
            if r.random() < 0.5:
                out = out.replace("LIMITED", "LTD")
            if out != name:
                return out
        return name + " INTL"

    # ---- assemble ------------------------------------------------------------------------------------------------------------
    def tables(self) -> Tables:
        ordered = sorted(self.tx, key=lambda t: (t["timestamp"], t["_k"]))
        final: Dict[int, str] = {}
        txs = []
        for n, t in enumerate(ordered, 1):
            final[t["_k"]] = f"T{n:07d}"
            txs.append({"id": final[t["_k"]], **{c: t[c] for c in TABLES["transactions"][1:]}, })
        for t in txs:
            t["amount"] = f"{t['amount']:.2f}"
        cases = sorted(self.cases, key=lambda c: (c["day"], c["account"], c["pattern"]))
        alerts, truth = [], []
        for n, c in enumerate(cases, 1):
            aid = f"AL{n:05d}"
            alerts.append({"id": aid, "rule": c["rule"], "account": c["account"], "date": (self.base + timedelta(days=c["day"])).isoformat(),
                           "amount": f"{c['amount']:.2f}", "risk_score": c["score"]})
            truth.append({"alert_id": aid, "label": c["label"], "pattern": c["pattern"], "involved_accounts": LIST_SEP.join(c["involved"]),
                          "key_transactions": LIST_SEP.join(sorted(final[k] for k in c["keys"])), "reason": c["reason"]})
        sanctions = [{"id": f"S{i:04d}", **s} for i, s in enumerate(self.sanctions, 1)]
        customers = [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.customers]
        for c in customers:
            self.kyc[c["id"]] = self.profile(c)
        return {"customers": customers, "accounts": self.accounts, "transactions": txs, "alerts": alerts, "ground_truth": truth,
                "kyc_profiles": [{"customer_id": cid, "profile": text} for cid, text in sorted(self.kyc.items())], "sanctions_hits": sanctions}

    def profile(self, c: Dict[str, Any]) -> str:
        cur = self.cur[self.primary[c["id"]]]
        if c["type"] == "business":
            who = f"{c['name']} is {'an' if c['occupation'][0] in 'aeiou' else 'a'} {c['occupation']} business in {N.COUNTRY_NAMES.get(c['country'], c['country'])}."
        else:
            job = {"student": "is a student", "retired": "is retired"}.get(c["occupation"], f"works as {'an' if c['occupation'][0] in 'aeiou' else 'a'} {c['occupation']}")
            who = f"{c['name']} lives in {N.COUNTRY_NAMES.get(c['country'], c['country'])} and {job}."
        return (f"{who} Customer since {c['onboarding_date']}. Expects about {c['expected_monthly_activity']:,} {cur} of deposits each month. "
                f"Risk rating: {c['risk_rating']}.")


class Builtin(Generator):
    id = "builtin"

    def generate(self, seed: Any = 42, size: str = "small", jurisdiction: str = "ca", base_date: str = "2025-01-01", **_: Any) -> Dataset:
        if size not in SIZES:
            raise ValueError(f"size must be one of {', '.join(SIZES)}")
        if jurisdiction not in ("ca", "us", "both"):
            raise ValueError("jurisdiction must be ca, us or both")
        b = _Build(int(seed), size, jurisdiction, date.fromisoformat(str(base_date)))
        b.make_customers()
        b.normal_activity()
        pool = b.mule_pool()
        for pattern in PATTERNS:
            for _ in range(b.cfg["per_kind"]):
                b.launder(pattern, pool)
        for kind in ("disguised_payroll", "disguised_supplies"):
            for _ in range(b.cfg["per_kind"]):
                b.disguised(kind, pool)
        for i in range(b.cfg["monitor"]):
            b.lookalike(("possible_fan_in", "possible_cash_structuring", "possible_cycle", "possible_rapid_in_out")[i % 4], pool)
        for i in range(b.cfg["dismiss"]):
            b.benign(i % 4)
        b.sanctions_cases()
        tables = b.tables()
        problems = validate(tables)
        if problems:
            raise ValueError("generated data failed validation: " + "; ".join(problems[:5]))
        return Dataset(tables, ["synthetic", "ofac-sdn"], {"generator": "builtin", "version": VERSION, "seed": int(seed), "size": size,
                                                              "jurisdiction": jurisdiction, "base_date": str(base_date),
                                                              "institution": "Maple Ridge Credit Union"})
