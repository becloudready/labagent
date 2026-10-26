"""Put the corpus together: authored documents, case files and (optionally) regulation chunks."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..schema import Tables
from . import documents as D
from . import regs


def build_documents(tables: Tables, jurisdiction: str) -> List[D.Doc]:
    return D.policy_documents(jurisdiction) + D.case_documents(tables, jurisdiction)


def build_chunks(tables: Tables, jurisdiction: str, with_regulations: bool = True, http: Optional[Callable[[str], bytes]] = None,
                 cache_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Policies and case files are chunked by section; regulations by section; transactions and alerts stay in the tables."""
    chunks = [c for d in build_documents(tables, jurisdiction) for c in D.to_chunks(d)]
    if with_regulations:
        chunks += regs.regulation_chunks(jurisdiction, http=http or regs.default_http, cache_dir=cache_dir)
    return chunks


def write_markdown(docs: List[D.Doc], out_dir: Path) -> List[Path]:
    """One .md file per document (policies, procedures, guides and cases), for tools that ingest a folder."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for d in docs:
        p = out_dir / f"{d.id}.md"
        p.write_text(D.to_markdown(d), encoding="utf-8")
        paths.append(p)
    return paths
