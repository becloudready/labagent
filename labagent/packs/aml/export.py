"""Write a generated dataset to a folder: CSV always, Parquet when pyarrow is installed, and provenance.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .gen import Dataset
from .licences import SOURCES
from .schema import write_csv, write_parquet

PROVENANCE = "provenance.json"


def provenance(ds: Dataset) -> Dict[str, Any]:
    return {"sources": [{"id": s, "licence": SOURCES[s]["licence"], "mode": SOURCES[s]["mode"]} for s in ds.sources], "info": ds.info}


def write_dataset(ds: Dataset, out_dir: Path, parquet: str = "auto") -> List[str]:
    """parquet: 'auto' (write if pyarrow is importable), 'yes' (fail if it is not) or 'no'. Returns the file names written."""
    out_dir = Path(out_dir)
    files = [p.name for p in write_csv(ds.tables, out_dir)]
    if parquet != "no":
        try:
            files += [p.name for p in write_parquet(ds.tables, out_dir)]
        except ImportError:
            if parquet == "yes":
                raise
    (out_dir / PROVENANCE).write_text(json.dumps(provenance(ds), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return files + [PROVENANCE]
