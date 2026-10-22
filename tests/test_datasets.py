import gzip
import json

import pytest

from labagent.datasets import DatasetError, build_snapshot, fetch_snapshot, registry, sha256_file

ROWS = [{"id": f"AAPL-2025-1A-{i}", "title": "t", "content": f"passage {i}", "meta": {"ticker": "AAPL", "company": "Apple", "form": "10-K",
         "fiscal_year": "2025", "item": "1A", "filed": "2025-10-31", "url": "https://www.sec.gov/x"}} for i in range(1, 4)]


@pytest.fixture
def chunks(tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in ROWS))
    return f


def test_snapshot_is_reproducible_and_self_describing(tmp_path, chunks):
    a = build_snapshot(chunks, "demo", "1", tmp_path / "a", built="2026-01-01")
    b = build_snapshot(chunks, "demo", "1", tmp_path / "b", built="2026-01-01")
    assert a["sha256"] == b["sha256"]                                  # same input, same bytes: the hash can be reviewed in a PR
    assert a["chunks"] == 3 and a["companies"] == ["AAPL"] and len(a["filings"]) == 1
    assert (tmp_path / "a" / "SHA256SUMS").read_text().split()[0] == a["sha256"]
    assert "@" not in json.dumps(a)


def test_duplicate_ids_are_refused(tmp_path):
    f = tmp_path / "d.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [ROWS[0], ROWS[0]]))
    with pytest.raises(DatasetError, match="duplicate"):
        build_snapshot(f, "demo", "1", tmp_path / "o")


def test_fetch_verifies_checksum_caches_and_falls_back_to_mirror(tmp_path, chunks):
    m = build_snapshot(chunks, "demo", "1", tmp_path / "out")
    good = tmp_path / "out" / m["file"]
    entry = {"url": "https://primary.example/x.gz", "mirrors": ["https://mirror.example/x.gz"], "sha256": m["sha256"]}
    calls = []

    def download(url, dest):
        calls.append(url)
        dest.write_bytes(b"tampered" if "primary" in url else good.read_bytes())
    rows = fetch_snapshot("demo", cache_dir=tmp_path / "cache", download=download, entry=entry)
    assert [r["id"] for r in rows] == [r["id"] for r in ROWS] and len(calls) == 2    # primary was tampered, mirror used
    fetch_snapshot("demo", cache_dir=tmp_path / "cache", download=download, entry=entry)
    assert len(calls) == 2                                                           # second read is served from the verified cache


def test_fetch_refuses_when_every_source_fails_the_checksum(tmp_path):
    entry = {"url": "https://primary.example/x.gz", "sha256": "0" * 64}
    with pytest.raises(DatasetError, match="matching checksum"):
        fetch_snapshot("demo", cache_dir=tmp_path, download=lambda u, d: d.write_bytes(b"nope"), entry=entry)
    assert not list(tmp_path.glob("*"))                                              # a bad download is deleted, never kept


def test_pinned_registry_entries_are_well_formed():
    for name, e in registry().items():
        assert e["url"].startswith("https://github.com/becloudready/labagent-datasets/releases/download/")
        assert len(e["sha256"]) == 64 and e["bytes"] > 0 and name.startswith("sec-10k-")


def test_snapshot_source_feeds_the_pack_without_a_sec_contact(tmp_path, monkeypatch):
    from labagent.packs.base import PackContext
    from labagent.packs.rag.sources.sec import SecSnapshot
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))
    (tmp_path / "c.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS))
    m = build_snapshot(tmp_path / "c.jsonl", "demo", "1", tmp_path / "o")
    from labagent import datasets
    monkeypatch.setattr(datasets, "registry", lambda: {"demo": {"url": "https://x.example/a.gz", "sha256": m["sha256"]}})
    ctx = PackContext("p", "n", {"dataset": "demo"},
                      services={"download": lambda u, d: d.write_bytes((tmp_path / "o" / m["file"]).read_bytes())})
    assert len(SecSnapshot().chunks(ctx)) == 3
