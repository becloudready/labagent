"""generator=snapshot: read a pinned, checksummed table snapshot instead of generating one."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ....datasets import DatasetError, default_http, fetch_table_snapshot
from ..schema import read_csv, validate
from . import Dataset, Generator


class Snapshot(Generator):
    id = "snapshot"

    def __init__(self, download: Callable[[str, Path], None] = default_http, entry: Optional[Dict[str, Any]] = None):
        self.download, self.entry = download, entry

    def generate(self, dataset: str = "aml-synthetic", **_: Any) -> Dataset:
        import json
        with tempfile.TemporaryDirectory() as tmp:
            folder = fetch_table_snapshot(dataset, Path(tmp), download=self.download, entry=self.entry)
            tables = read_csv(folder)
            prov = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
        problems = validate(tables)
        if problems:
            raise DatasetError("the snapshot failed validation: " + "; ".join(problems[:3]))
        return Dataset(tables, [s["id"] for s in prov["sources"]], {**prov.get("info", {}), "generator": "snapshot", "dataset": dataset})
