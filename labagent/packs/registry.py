"""Pack discovery: built-ins first, then the `labagent.packs` entry-point group."""
from __future__ import annotations

from typing import Dict

from .base import Pack


def builtin() -> Dict[str, Pack]:
    from .rag.pack import RagLakebase, RagSecFilings, RagSecFilingsLive
    from .db_agent.pack import DbAgent
    from .rag_ide.pack import RagIde
    return {p.id: p for p in (RagLakebase(), RagSecFilings(), RagSecFilingsLive(), DbAgent(), RagIde())}


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


def resolve(token: str) -> Pack:
    """Exact id, then alias, then a unique prefix. Ambiguity is an error, never a guess."""
    packs = all_packs()
    t = token.strip().lower()
    if t in packs:
        return packs[t]
    by_alias = [p for p in packs.values() if t in [a.lower() for a in p.aliases]]
    if len(by_alias) == 1:
        return by_alias[0]
    pref = [p for p in packs.values() if p.id.startswith(t)]
    if len(pref) == 1:
        return pref[0]
    options = sorted(p.id for p in (pref or packs.values()))
    raise KeyError(f"{token!r} is {'ambiguous' if pref else 'not a known pack'}. Options: {', '.join(options)}")
