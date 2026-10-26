"""python -m labagent.packs.aml.gen --out DIR [--seed 42] [--size small] [--jurisdiction ca] [--parquet auto]"""
import argparse
import json
from pathlib import Path

from ..export import write_dataset
from ..schema import table_counts
from . import get


def main() -> int:
    p = argparse.ArgumentParser(description="Generate the synthetic AML tables")
    p.add_argument("--out", required=True)
    p.add_argument("--generator", default="builtin")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--size", default="small", choices=["tiny", "small", "medium"])
    p.add_argument("--jurisdiction", default="ca", choices=["ca", "us", "both"])
    p.add_argument("--parquet", default="auto", choices=["auto", "yes", "no"])
    a = p.parse_args()
    ds = get(a.generator).generate(seed=a.seed, size=a.size, jurisdiction=a.jurisdiction)
    files = write_dataset(ds, Path(a.out), a.parquet)
    print(json.dumps({"files": files, "rows": table_counts(ds.tables)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
