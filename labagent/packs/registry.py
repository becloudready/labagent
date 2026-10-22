"""Pack discovery: built-ins first, then the `labagent.packs` entry-point group."""
from __future__ import annotations

from typing import Dict

from .base import Pack


def builtin() -> Dict[str, Pack]:
    from .rag.pack import RagLakebase, RagSecFilings, RagSecFilingsLive
    return {p.id: p for p in (RagLakebase(), RagSecFilings(), RagSecFilingsLive())}


def all_packs() -> Dict[str, Pack]:
    found = builtin()
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = eps.select(group="labagent.packs") if hasattr(eps, "select") else eps.get("labagent.packs", [])
        for ep in group:
            pack = ep.load()()
            found.setdefault(pack.id, pack)   # a built-in id can never be shadowed by a plugin
    except Exception:
        pass
    return found


def get(pack_id: str) -> Pack:
    packs = all_packs()
    if pack_id not in packs:
        raise KeyError(f"unknown pack '{pack_id}'. Available: {', '.join(sorted(packs))}")
    return packs[pack_id]
