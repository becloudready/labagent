"""Data sources for RAG packs. A source turns an upstream corpus into chunks (id, title, content, meta)
plus an evaluation set. Add one by subclassing Source; register built-ins below or via the
`labagent.rag_sources` entry point."""
from __future__ import annotations

from typing import Any, Dict, List

from ...base import PackContext, Param


class Source:
    id: str = ""
    params: List[Param] = []
    schema: str = ""          # Postgres schema for this source's table: keeps sources side by side in one project

    def chunks(self, ctx: PackContext) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def eval_set(self) -> List[Dict[str, Any]]:
        """[{question, expect: {meta_key: value}}]: a retrieved chunk is a hit when its meta contains `expect`."""
        return []


def _builtin() -> Dict[str, Source]:
    from .builtin import Builtin
    from .sec import SecFilings
    return {s.id: s for s in (Builtin(), SecFilings())}


def get(source_id: str) -> Source:
    found = _builtin()
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = eps.select(group="labagent.rag_sources") if hasattr(eps, "select") else eps.get("labagent.rag_sources", [])
        for ep in group:
            s = ep.load()()
            found.setdefault(s.id, s)
    except Exception:
        pass
    if source_id not in found:
        raise KeyError(f"unknown RAG source '{source_id}'. Available: {', '.join(sorted(found))}")
    return found[source_id]
