"""SQL notebooks as the single source of truth.

Each file under `sql/<domain>/` is a Databricks SQL notebook (`-- Databricks notebook source`, cells split by `-- COMMAND ----------`).
The pack runs its cells one statement at a time, and uploads the very same files for the learner to read and re-run.

- A cell whose lines all start with `-- MAGIC` is markdown: shown to the learner, never run.
- A cell that holds only comments is skipped.
- A line `-- @optional` marks a statement that may fail without stopping the build (tags, masks, constraints): the failure is reported.
- `__NAME__` placeholders are replaced before anything runs or is uploaded, so the learner sees real catalog and group names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

HEADER = "-- Databricks notebook source"
SEPARATOR = re.compile(r"^-- COMMAND ----------\s*$", re.M)
OPTIONAL = "-- @optional"
PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")


@dataclass(frozen=True)
class Cell:
    sql: str
    optional: bool = False

    @property
    def label(self) -> str:
        first = next((ln.strip() for ln in self.sql.splitlines() if ln.strip() and not ln.strip().startswith("--")), "")
        return re.sub(r"\s+", " ", first)[:70]


def substitute(text: str, values: Dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    left = PLACEHOLDER.findall(text)
    if left:
        raise ValueError(f"unfilled placeholder(s): {', '.join(sorted(set(left)))}")
    return text


def cells(text: str) -> List[Cell]:
    out: List[Cell] = []
    for raw in SEPARATOR.split(text.replace(HEADER, "", 1)):
        lines = [ln for ln in raw.strip().splitlines() if ln.strip()]
        if not lines or all(ln.startswith("-- MAGIC") for ln in lines):
            continue
        if all(ln.strip().startswith("--") for ln in lines):
            continue
        optional = any(ln.strip() == OPTIONAL for ln in lines)
        statement = "\n".join(ln for ln in lines if ln.strip() != OPTIONAL).strip().rstrip(";").strip()
        out.append(Cell(statement, optional))
    return out
