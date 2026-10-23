"""Structure-aware chunking: pack whole paragraphs up to a size, with a small paragraph overlap.
Sections are never merged, so every chunk belongs to exactly one section and carries its metadata."""
from __future__ import annotations

import re
from typing import Dict, List

PARA = re.compile(r"\n\s*\n|\n(?=\S)")


def split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in PARA.split(text) if p.strip()]


def chunk_text(text: str, max_chars: int = 1200, overlap_paras: int = 1) -> List[str]:
    """Greedy paragraph packing. A paragraph longer than max_chars is split on sentence boundaries."""
    units: List[str] = []
    for p in split_paragraphs(text):
        if len(p) <= max_chars:
            units.append(p)
            continue
        cur = ""
        for s in re.split(r"(?<=[.!?])\s+", p):
            if cur and len(cur) + len(s) + 1 > max_chars:
                units.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            units.append(cur)
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    for u in units:
        if cur and size + len(u) + 1 > max_chars:
            chunks.append("\n".join(cur))
            tail = cur[-overlap_paras:] if overlap_paras else []
            cur = tail if sum(len(t) for t in tail) < max_chars // 3 else []
            size = sum(len(x) + 1 for x in cur)
        cur.append(u)
        size += len(u) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def make_chunks(doc_id: str, title: str, text: str, meta: Dict[str, str], **kw) -> List[Dict]:
    return [{"id": f"{doc_id}-{i}", "title": title, "content": c, "meta": meta}
            for i, c in enumerate(chunk_text(text, **kw), 1)]
