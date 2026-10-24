"""AML pack, part 1: the builtin generator, the schema, the licence gate and table snapshots."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from labagent.datasets import DatasetError, build_table_snapshot, fetch_table_snapshot, sha256_file
from labagent.packs.aml import names as N
from labagent.packs.aml.export import write_dataset
from labagent.packs.aml.gen import get
from labagent.packs.aml.licences import SOURCES, LicenceError, assert_redistributable
from labagent.packs.aml.schema import (ALL_PATTERNS, DISGUISED, EXTERNAL_PREFIX, INSTITUTION, LABELS, PATTERNS, TABLES, read_csv,
                                       table_counts, validate, write_csv)

ROOT = Path(__file__).resolve().parents[1]


def make(**kw):
    kw.setdefault("seed", 42)
    kw.setdefault("size", "tiny")
    return get("builtin").generate(**kw)


@pytest.fixture(scope="module")
def small():
    return make(size="small")


# ---- determinism and schema ---------------------------------------------------------------------------------------------------

def test_same_seed_gives_identical_files_and_a_different_seed_does_not(tmp_path):
    for d, seed in (("a", 7), ("b", 7), ("c", 8)):
        write_dataset(make(seed=seed), tmp_path / d, parquet="no")
    same = lambda x, y: all(sha256_file(tmp_path / x / f) == sha256_file(tmp_path / y / f) for f in sorted(p.name for p in (tmp_path / x).iterdir()))
    assert same("a", "b") and not same("a", "c")


def test_every_table_has_exactly_the_schema_columns_and_validates(small):
    assert validate(small.tables) == []
    for name, cols in TABLES.items():
        assert small.tables[name] and list(small.tables[name][0].keys()) == cols


def test_csv_round_trip_still_validates(tmp_path, small):
    write_csv(small.tables, tmp_path)
    assert validate(read_csv(tmp_path)) == []


def test_validate_catches_broken_data(small):
    broken = {k: [dict(r) for r in v] for k, v in small.tables.items()}
    broken["transactions"][0]["to_account"] = "ACC999999"
    broken["ground_truth"][0]["label"] = "maybe"
    broken["sanctions_hits"][0]["note"] = "real"
    msgs = " ".join(validate(broken))
    assert "unknown to_account" in msgs and "bad label" in msgs and "TEST DATA" in msgs


def test_sizes_and_unknown_options_are_checked():
    assert table_counts(make(size="tiny").tables)["customers"] == 60
    with pytest.raises(ValueError):
        make(size="huge")
    with pytest.raises(ValueError):
        make(jurisdiction="fr")


def test_jurisdiction_sets_the_home_currency():
    ca = {t["currency"] for t in make(jurisdiction="ca").tables["transactions"]}
    us = {t["currency"] for t in make(jurisdiction="us").tables["transactions"]}
    assert "CAD" in ca and "USD" in us
    assert {c["country"] for c in make(jurisdiction="both", size="small").tables["customers"]} >= {"CA", "US"}


# ---- labels --------------------------------------------------------------------------------------------------------------------

def test_each_of_the_eight_patterns_appears_labelled_escalate(small):
    escalate = {g["pattern"] for g in small.tables["ground_truth"] if g["label"] == "escalate"}
    assert set(PATTERNS) <= escalate and set(DISGUISED) <= escalate and "sanctions_match" in escalate


def test_all_three_labels_exist_and_follow_the_rules_of_construction(small):
    gt = small.tables["ground_truth"]
    assert {g["label"] for g in gt} == set(LABELS)
    for g in gt:
        if g["label"] == "dismiss":
            assert g["pattern"] == "none"
        if g["label"] == "monitor":
            assert g["pattern"].startswith("possible_") or g["pattern"] == "sanctions_match"
        assert g["pattern"] in ALL_PATTERNS and g["reason"].strip() and g["key_transactions"]


def test_graph_patterns_have_the_right_shape(small):
    tx = {t["id"]: t for t in small.tables["transactions"]}
    gt = [g for g in small.tables["ground_truth"] if g["label"] == "escalate"]
    for g in gt:
        legs = [tx[k] for k in g["key_transactions"].split(";")]
        senders, receivers = {t["from_account"] for t in legs}, {t["to_account"] for t in legs}
        if g["pattern"] == "fan_out":
            hub = max(senders, key=lambda s: sum(1 for t in legs if t["from_account"] == s))
            assert len({t["to_account"] for t in legs if t["from_account"] == hub}) >= 4
        if g["pattern"] == "fan_in":
            assert len(senders) >= 4 and len(receivers) == 1
        if g["pattern"] == "cycle":
            assert senders == receivers and len(legs) == len(senders)       # every account both sends and receives, once
        if g["pattern"] == "bipartite":
            assert not (senders & receivers)
        if g["pattern"] == "scatter_gather":
            assert len(senders - receivers) == 1 and len(receivers - senders) == 1


def test_disguised_cases_use_ordinary_looking_descriptions(small):
    tx = {t["id"]: t for t in small.tables["transactions"]}
    for g in small.tables["ground_truth"]:
        if g["pattern"] == "disguised_payroll":
            assert any(tx[k]["description"] == "PAYROLL" for k in g["key_transactions"].split(";"))
        if g["pattern"] == "disguised_supplies":
            assert any(tx[k]["description"].startswith("SUPPLIES INVOICE") and 9000 < float(tx[k]["amount"]) < 10000
                       for k in g["key_transactions"].split(";"))


def test_laundering_does_not_match_the_customer_profile(small):
    """The point of the lab: the alert account moved far more than its KYC profile says to expect."""
    expected = {c["id"]: int(c["expected_monthly_activity"]) for c in small.tables["customers"]}
    owner = {a["id"]: a["customer_id"] for a in small.tables["accounts"]}
    alert = {a["id"]: a for a in small.tables["alerts"]}
    ratios = [float(alert[g["alert_id"]]["amount"]) / expected[owner[alert[g["alert_id"]]["account"]]]
              for g in small.tables["ground_truth"] if g["label"] == "escalate" and g["pattern"] in PATTERNS]
    assert sum(r > 2 for r in ratios) / len(ratios) > 0.8


def test_no_case_happens_before_its_account_was_opened(small):
    opened = {a["id"]: a["open_date"] for a in small.tables["accounts"]}
    tx = {t["id"]: t for t in small.tables["transactions"]}
    for g in small.tables["ground_truth"]:
        for k in g["key_transactions"].split(";"):
            for side in ("from_account", "to_account"):
                a = tx[k][side]
                if a in opened:
                    assert tx[k]["timestamp"][:10] >= opened[a]


def test_case_reasons_match_the_data():
    """Reasons are written for learners to check against the tables, so the checkable claims must be true."""
    for seed in (42, 1, 2):
        ds = make(seed=seed, size="small")
        tx = {t["id"]: t for t in ds.tables["transactions"]}
        allx = ds.tables["transactions"]
        for g in ds.tables["ground_truth"]:
            legs = [tx[k] for k in g["key_transactions"].split(";")]
            if g["pattern"] == "disguised_supplies":
                inn = sum(float(t["amount"]) for t in legs if t["description"].startswith("INVOICE"))
                out = sum(float(t["amount"]) for t in legs if t["description"].startswith("SUPPLIES"))
                assert out <= inn + 0.01
            if g["label"] == "dismiss" and legs[0]["description"] == "FAMILY SUPPORT":
                payee, amt = legs[0]["to_account"], legs[0]["amount"]
                earlier = [t for t in allx if t["to_account"] == payee and t["amount"] == amt and t["timestamp"] < legs[0]["timestamp"]]
                assert len(earlier) >= 2
            if g["pattern"] == "possible_cash_structuring":
                assert all(float(t["amount"]) > 8000 for t in legs)


# ---- sanctions seeds -----------------------------------------------------------------------------------------------------------

def test_sanctions_hits_are_tagged_test_data_and_use_entity_names_only(small):
    seed = json.loads((ROOT / "labagent/packs/aml/data/sdn_seed.json").read_text())
    assert len(seed["entries"]) == 12 and all(set(e) == {"uid", "name", "program"} and e["uid"].isdigit() for e in seed["entries"])   # no addresses, no people
    hits = small.tables["sanctions_hits"]
    assert {h["match_type"] for h in hits} == {"exact", "fuzzy"}
    for h in hits:
        assert "TEST DATA" in h["note"] and h["list_date"] == seed["list_date"]
        assert (h["customer_name"] == h["sdn_name"]) == (h["match_type"] == "exact")
        assert h["sdn_name"] in {e["name"] for e in seed["entries"]}


# ---- names and hygiene ---------------------------------------------------------------------------------------------------------

def test_no_real_bank_or_payment_brand_in_generated_tables_or_shipped_code(small):
    for rows in small.tables.values():
        assert N.real_bank_hits(json.dumps(rows)) == []
    assert INSTITUTION == "Maple Ridge Credit Union"
    for f in (ROOT / "labagent/packs/aml").rglob("*.py"):
        if f.name != "names.py":
            assert N.real_bank_hits(f.read_text()) == [], f


def test_denylist_catches_what_it_should():
    assert N.real_bank_hits("sent via Interac to a TD Bank account at Scotiabank") == ["interac", "scotiabank", "td bank"]
    assert N.real_bank_hits("Maple Ridge Credit Union") == []


def test_generated_files_pass_the_repo_pii_scan(tmp_path):
    """scripts/check_no_pii.py only reads tracked files, so run the same patterns over a generated folder."""
    write_dataset(make(size="small"), tmp_path, parquet="no")
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_no_pii as c
    finally:
        sys.path.pop(0)
    bad = []
    for f in tmp_path.iterdir():
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if any(not c.ALLOWED_EMAIL.search(m.group(0)) for m in c.EMAIL.finditer(line)):
                bad.append(f"{f.name}:{n} email")
            if c.ACCOUNT.search(c.UUID.sub("<uuid>", line)):
                bad.append(f"{f.name}:{n} 12-digit number")
            if c.KEYS.search(line):
                bad.append(f"{f.name}:{n} key")
    assert bad == []


def test_ids_are_short_and_external_parties_are_marked(small):
    assert all(len(a["id"]) == 9 for a in small.tables["accounts"])
    ext = {t[s] for t in small.tables["transactions"] for s in ("from_account", "to_account") if t[s].startswith(EXTERNAL_PREFIX)}
    assert ext and not ext & {a["id"] for a in small.tables["accounts"]}


# ---- licences and snapshots ----------------------------------------------------------------------------------------------------

def test_only_bundled_sources_may_enter_a_snapshot():
    assert_redistributable(["synthetic", "ofac-sdn", "ecfr-31-cfr-x", "justice-laws-ca"])
    for s in ("opensanctions", "fintrac", "ffiec-bsa-aml", "ibm-aml-kaggle", "saml-d"):
        assert SOURCES[s]["mode"] != "bundled"
        with pytest.raises(LicenceError):
            assert_redistributable(["synthetic", s])
    with pytest.raises(LicenceError):
        assert_redistributable(["something-new"])


def test_builtin_data_uses_only_bundled_sources():
    ds = make()
    assert ds.sources == ["synthetic", "ofac-sdn"]
    assert "opensanctions" not in json.dumps(ds.info).lower()


def test_snapshot_is_reproducible_and_lists_its_sources(tmp_path):
    for d in "ab":
        write_dataset(make(), tmp_path / f"src-{d}", parquet="no")
    a = build_table_snapshot(tmp_path / "src-a", "aml-synthetic", "t1", tmp_path / "a", built="2026-01-01")
    b = build_table_snapshot(tmp_path / "src-b", "aml-synthetic", "t1", tmp_path / "b", built="2026-01-01")
    assert a["sha256"] == b["sha256"] and a["kind"] == "tables"
    assert [s["id"] for s in a["sources"]] == ["synthetic", "ofac-sdn"] and set(a["files"]) >= {"transactions.csv", "provenance.json"}
    assert (tmp_path / "a" / "SHA256SUMS").read_text().split()[0] == a["sha256"]


def test_snapshot_refuses_a_fetch_only_source_and_missing_provenance(tmp_path):
    write_dataset(make(), tmp_path / "src", parquet="no")
    prov = json.loads((tmp_path / "src" / "provenance.json").read_text())
    prov["sources"].append({"id": "opensanctions", "licence": "CC-BY-NC-4.0", "mode": "fetched"})
    (tmp_path / "src" / "provenance.json").write_text(json.dumps(prov))
    with pytest.raises(DatasetError, match="opensanctions"):
        build_table_snapshot(tmp_path / "src", "x", "1", tmp_path / "out")
    (tmp_path / "src" / "provenance.json").unlink()
    with pytest.raises(DatasetError, match="provenance"):
        build_table_snapshot(tmp_path / "src", "x", "1", tmp_path / "out")


def test_fetch_checks_the_hash_and_unpacks_only_flat_files(tmp_path):
    write_dataset(make(), tmp_path / "src", parquet="no")
    m = build_table_snapshot(tmp_path / "src", "demo", "1", tmp_path / "out")
    entry = {"kind": "tables", "url": "https://x.example/demo.zip", "sha256": m["sha256"]}
    zbytes = (tmp_path / "out" / m["file"]).read_bytes()
    dl = lambda u, d: d.write_bytes(zbytes)
    dest = fetch_table_snapshot("demo", tmp_path / "dest", cache_dir=tmp_path / "cache", download=dl, entry=entry)
    assert validate(read_csv(dest)) == []
    with pytest.raises(DatasetError, match="matching checksum"):
        fetch_table_snapshot("demo", tmp_path / "d2", cache_dir=tmp_path / "c2", download=lambda u, d: d.write_bytes(b"tampered"), entry=entry)


def test_zip_with_a_path_in_it_is_refused(tmp_path):
    import zipfile
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../escape.csv", "x")
    entry = {"kind": "tables", "url": "https://x.example/evil.zip", "sha256": sha256_file(z)}
    with pytest.raises(DatasetError, match="unsafe"):
        fetch_table_snapshot("evil", tmp_path / "d", cache_dir=tmp_path / "c", download=lambda u, d: d.write_bytes(z.read_bytes()), entry=entry)
    assert not (tmp_path / "escape.csv").exists()


def test_table_and_chunk_snapshots_are_not_mixed_up(tmp_path):
    from labagent.datasets import fetch_snapshot
    with pytest.raises(DatasetError, match="tables"):
        fetch_snapshot("t", cache_dir=tmp_path, entry={"kind": "tables", "url": "https://x.example/a.zip", "sha256": "0" * 64})


def test_dataset_cli_packages_tables(tmp_path, capsys):
    from labagent.cli import main
    write_dataset(make(), tmp_path / "src", parquet="no")
    assert main(["dataset", "--tables", str(tmp_path / "src"), "--name", "aml-synthetic", "--version", "t1", "--out", str(tmp_path / "o")]) == 0
    assert json.loads(capsys.readouterr().out)["file"] == "aml-synthetic-t1.tables.zip"
    assert main(["dataset", "--name", "x", "--version", "1", "--out", str(tmp_path / "o")]) == 2


def test_module_entry_point_writes_the_tables(tmp_path):
    p = subprocess.run([sys.executable, "-m", "labagent.packs.aml.gen", "--out", str(tmp_path), "--size", "tiny", "--parquet", "no"],
                       capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0 and json.loads(p.stdout)["rows"]["customers"] == 60
