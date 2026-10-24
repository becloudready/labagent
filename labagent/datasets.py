"""Versioned, checksum-pinned dataset snapshots.

A snapshot is a gzip'd JSONL of chunks (id, title, content, meta) plus a manifest. Snapshots are published as GitHub
release assets; the pinned URL and SHA-256 live in `labagent/packs/rag/sources/datasets.json`, committed with the code,
so changing the data is a reviewed code change. A download whose hash differs is refused and deleted.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__


class DatasetError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_snapshot(chunks_path: Path, name: str, version: str, out_dir: Path, source: str = "SEC EDGAR",
                   built: Optional[str] = None) -> Dict[str, Any]:
    """Write <name>-<version>.chunks.jsonl.gz (byte-for-byte reproducible), a manifest and SHA256SUMS."""
    rows = [json.loads(l) for l in Path(chunks_path).read_text().splitlines() if l.strip()]
    if not rows:
        raise DatasetError("no chunks to package")
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise DatasetError("duplicate chunk ids")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{version}"
    data = out_dir / f"{stem}.chunks.jsonl.gz"
    body = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in rows) + "\n"
    with open(data, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(body.encode("utf-8"))
    filings = {}
    for r in rows:
        m = r["meta"]
        if "url" in m and "ticker" in m:
            filings[m["url"]] = {k: m.get(k) for k in ("ticker", "company", "form", "fiscal_year", "filed", "url")}
    sources = sorted({r["meta"]["source"] for r in rows if r["meta"].get("source")})
    if sources:      # chunks that name their source (the AML corpus) must come from sources that may be redistributed
        from .packs.aml.licences import assert_redistributable
        try:
            assert_redistributable(sources)
        except Exception as e:
            data.unlink(missing_ok=True)
            raise DatasetError(str(e)) from e
    manifest = {
        "name": name, "version": version, "source": source, "built": built or date.today().isoformat(),
        "labagent": __version__, "chunks": len(rows), "file": data.name, "sha256": sha256_file(data), "bytes": data.stat().st_size,
        "companies": sorted({f["ticker"] for f in filings.values() if f.get("ticker")}),
        "filings": sorted(filings.values(), key=lambda f: (f["ticker"], f["fiscal_year"])),
        "items": sorted({r["meta"].get("item") for r in rows if r["meta"].get("item")}),
    }
    if sources:
        manifest["sources"] = sources
    (out_dir / f"{stem}.manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out_dir / "SHA256SUMS").write_text(f"{manifest['sha256']}  {data.name}\n")
    return manifest


def registry() -> Dict[str, Dict[str, Any]]:
    return json.loads((Path(__file__).parent / "packs" / "rag" / "sources" / "datasets.json").read_text())


def default_http(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"labagent/{__version__}"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        for block in iter(lambda: r.read(1 << 20), b""):
            f.write(block)


def fetch_snapshot(name: str, cache_dir: Optional[Path] = None, download: Callable[[str, Path], None] = default_http,
                   entry: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return the chunks of a pinned snapshot, downloading once and verifying the SHA-256 every time it is read."""
    entry = entry or registry().get(name)
    if not entry:
        raise DatasetError(f"unknown dataset '{name}'. Known: {', '.join(sorted(registry()))}")
    if entry.get("kind", "chunks") != "chunks":
        raise DatasetError(f"dataset '{name}' holds tables, not chunks: use fetch_table_snapshot")
    root = cache_dir or Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "cache" / "datasets"
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{entry['sha256'][:16]}-{Path(entry['url']).name}"
    if not dest.exists() or sha256_file(dest) != entry["sha256"]:
        errors = []
        for url in [entry["url"], *entry.get("mirrors", [])]:
            tmp = dest.with_suffix(".part")
            try:
                download(url, tmp)
                if sha256_file(tmp) != entry["sha256"]:
                    raise DatasetError("checksum mismatch")
                tmp.replace(dest)
                break
            except Exception as e:
                tmp.unlink(missing_ok=True)
                errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
        else:
            raise DatasetError(f"could not obtain dataset '{name}' with a matching checksum ({'; '.join(errors)})")
    with gzip.open(dest, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ---- table snapshots (relational data such as the AML pack's CSV files) ---------------------------------------------------------
# A table snapshot is one zip of flat files (CSV, Parquet, provenance.json) plus a manifest and SHA256SUMS. The zip is reproducible
# byte for byte. Which sources may go in is decided by labagent.packs.aml.licences, not here: this function only asks.

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def build_table_snapshot(src_dir: Path, name: str, version: str, out_dir: Path, built: Optional[str] = None) -> Dict[str, Any]:
    from .packs.aml.licences import assert_redistributable
    src_dir = Path(src_dir)
    prov_path = src_dir / "provenance.json"
    if not prov_path.exists():
        raise DatasetError("provenance.json is missing: a snapshot must say where its data came from")
    prov = json.loads(prov_path.read_text())
    try:
        assert_redistributable([s["id"] for s in prov.get("sources", [])])
    except Exception as e:
        raise DatasetError(str(e)) from e
    files = sorted(p for p in src_dir.iterdir() if p.is_file())
    if not files:
        raise DatasetError("no files to package")
    for p in files:
        if not SAFE_NAME.match(p.name):
            raise DatasetError(f"unsafe file name: {p.name}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{version}"
    data = out_dir / f"{stem}.tables.zip"
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            info = zipfile.ZipInfo(p.name, ZIP_TIME)
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(info, p.read_bytes(), compresslevel=9)
    manifest = {
        "name": name, "version": version, "kind": "tables", "built": built or date.today().isoformat(), "labagent": __version__,
        "file": data.name, "sha256": sha256_file(data), "bytes": data.stat().st_size,
        "files": {p.name: sha256_file(p) for p in files}, "sources": prov["sources"], "info": prov.get("info", {}),
    }
    (out_dir / f"{stem}.manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out_dir / "SHA256SUMS").write_text(f"{manifest['sha256']}  {data.name}\n")
    return manifest


def fetch_table_snapshot(name: str, dest: Path, cache_dir: Optional[Path] = None, download: Callable[[str, Path], None] = default_http,
                         entry: Optional[Dict[str, Any]] = None) -> Path:
    """Download a pinned table snapshot once, check its SHA-256 every time, and unpack the flat files into `dest`."""
    entry = entry or registry().get(name)
    if not entry or entry.get("kind") != "tables":
        raise DatasetError(f"unknown table dataset '{name}'")
    root = cache_dir or Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "cache" / "datasets"
    root.mkdir(parents=True, exist_ok=True)
    zpath = root / f"{entry['sha256'][:16]}-{Path(entry['url']).name}"
    if not zpath.exists() or sha256_file(zpath) != entry["sha256"]:
        errors = []
        for url in [entry["url"], *entry.get("mirrors", [])]:
            tmp = zpath.with_suffix(".part")
            try:
                download(url, tmp)
                if sha256_file(tmp) != entry["sha256"]:
                    raise DatasetError("checksum mismatch")
                tmp.replace(zpath)
                break
            except Exception as e:
                tmp.unlink(missing_ok=True)
                errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
        else:
            raise DatasetError(f"could not obtain dataset '{name}' with a matching checksum ({'; '.join(errors)})")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if not SAFE_NAME.match(info.filename):         # no paths, no '..': a snapshot is a flat folder
                raise DatasetError(f"refusing unsafe name in snapshot: {info.filename!r}")
            (dest / info.filename).write_bytes(z.read(info))
    return dest
