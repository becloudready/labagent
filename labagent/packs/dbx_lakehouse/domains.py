"""The datasets the lakehouse pack can build. Adding a domain means a snapshot in datasets.json, a folder of SQL notebooks and an entry here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Tuple

LAYERS = ("bronze", "silver", "gold")


@dataclass(frozen=True)
class Domain:
    id: str
    title: str
    dataset: str                                   # key in labagent/packs/rag/sources/datasets.json
    notebooks: Tuple[str, ...]                     # run in this order; the last may be governance
    governance: str                                # notebook run after the checks ("" for none)
    reader_layers: Tuple[str, ...]                 # layers learners may read
    volume_readable: bool                          # may learners read the landing volume (no sensitive raw files)
    bronze_tables: Callable[[Dict[str, int]], Dict[str, int]]   # expected bronze rows per table, from the snapshot's row counts
    silver_gold: Dict[str, Tuple[str, ...]]        # tables that must exist and hold rows
    invariants: Tuple[Tuple[str, str], ...]        # (what must be true, SQL that returns true). __CATALOG__ is replaced
    append_only: Tuple[str, ...] = ()              # bronze tables filled with COPY INTO: dropped before a rebuild

    def schema(self, layer: str) -> str:
        return f"{self.id}_{layer}"


def _retail_bronze(rows: Dict[str, int]) -> Dict[str, int]:
    return {"invoice_lines_raw": sum(n for f, n in rows.items() if "_initial_" in f)}


def _healthcare_bronze(rows: Dict[str, int]) -> Dict[str, int]:
    return {f"{f[len('healthcare_'):-len('.csv')]}_raw": n for f, n in rows.items() if f.startswith("healthcare_") and f.endswith(".csv")}


DOMAINS: Dict[str, Domain] = {d.id: d for d in (
    Domain(
        id="retail", title="Online retail (UCI Online Retail II, CC BY 4.0)", dataset="lakehouse-retail-2026.10",
        notebooks=("01_bronze.sql", "02_silver.sql", "03_gold.sql"), governance="",
        reader_layers=("bronze", "silver", "gold"), volume_readable=True, bronze_tables=_retail_bronze,
        silver_gold={"silver": ("invoice_lines", "invoice_lines_rejected", "customers", "products"),
                     "gold": ("daily_sales", "monthly_revenue", "customer_rfm", "product_performance")},
        invariants=(("every bronze line is either in silver or in the rejected table",
                     "SELECT (SELECT count(*) FROM __CATALOG__.retail_silver.invoice_lines) + "
                     "(SELECT count(*) FROM __CATALOG__.retail_silver.invoice_lines_rejected) = "
                     "(SELECT count(*) FROM __CATALOG__.retail_bronze.invoice_lines_raw)"),
                    ("gold net revenue equals silver net revenue",
                     "SELECT (SELECT sum(net_revenue) FROM __CATALOG__.retail_gold.daily_sales) = "
                     "(SELECT sum(line_total) FROM __CATALOG__.retail_silver.invoice_lines)")),
        append_only=("invoice_lines_raw",),
    ),
    Domain(
        id="healthcare", title="Healthcare (Synthea synthetic patients, public domain)", dataset="lakehouse-healthcare-2026.10",
        notebooks=("01_bronze.sql", "02_silver.sql", "03_gold.sql"), governance="04_governance.sql",
        reader_layers=("silver", "gold"), volume_readable=False, bronze_tables=_healthcare_bronze,
        silver_gold={"silver": ("patients", "encounters", "conditions", "medications", "observations", "procedures", "immunizations",
                                "allergies", "careplans", "organizations", "providers", "payers", "claims"),
                     "gold": ("patient_360", "encounters_by_month_class", "condition_prevalence", "payer_performance", "medication_utilization")},
        invariants=(("silver has one row per raw patient",
                     "SELECT (SELECT count(*) FROM __CATALOG__.healthcare_silver.patients) = "
                     "(SELECT count(DISTINCT Id) FROM __CATALOG__.healthcare_bronze.patients_raw)"),
                    ("patient_360 has one row per patient",
                     "SELECT (SELECT count(*) FROM __CATALOG__.healthcare_gold.patient_360) = "
                     "(SELECT count(*) FROM __CATALOG__.healthcare_silver.patients)")),
    ),
)}


def landing_folder(filename: str) -> str:
    """Where a snapshot file goes inside the landing volume: month files for incremental loading are split by name."""
    if "_initial_" in filename:
        return "initial"
    if "_incoming_" in filename:
        return "incoming"
    return ""
