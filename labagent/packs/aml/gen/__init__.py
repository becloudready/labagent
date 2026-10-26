"""Generators. Each one returns a `Dataset`: the seven tables in `schema.TABLES` plus a record of which sources it used."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..schema import Tables


@dataclass
class Dataset:
    tables: Tables
    sources: List[str]                                   # ids from licences.SOURCES
    info: Dict[str, Any] = field(default_factory=dict)   # generator name, seed, size, version: all non-secret


class Generator:
    id = ""

    def generate(self, **params: Any) -> Dataset:
        raise NotImplementedError


def get(generator_id: str) -> Generator:
    if generator_id == "builtin":
        from .builtin import Builtin
        return Builtin()
    if generator_id == "snapshot":
        from .snapshot import Snapshot
        return Snapshot()
    raise KeyError(f"unknown generator '{generator_id}'. Available now: builtin, snapshot")
