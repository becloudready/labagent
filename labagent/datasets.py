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
import urllib.request
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
        if "url" in m:
            filings[m["url"]] = {k: m.get(k) for k in ("ticker", "company", "form", "fiscal_year", "filed", "url")}
    manifest = {
        "name": name, "version": version, "source": source, "built": built or date.today().isoformat(),
        "labagent": __version__, "chunks": len(rows), "file": data.name, "sha256": sha256_file(data), "bytes": data.stat().st_size,
        "companies": sorted({f["ticker"] for f in filings.values() if f.get("ticker")}),
        "filings": sorted(filings.values(), key=lambda f: (f["ticker"], f["fiscal_year"])),
        "items": sorted({r["meta"].get("item") for r in rows if r["meta"].get("item")}),
    }
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
