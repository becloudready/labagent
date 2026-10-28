# Lakehouse lab: start here

You have a working lakehouse in Unity Catalog. Real open data has already been loaded and carried through the three medallion layers.
Your job is to read it, check it, break it safely, and extend it.

## Where everything is

| Where | What |
|---|---|
| Catalog `__CATALOG__` | All the tables |
| `<domain>_bronze` | Raw data exactly as it arrived, all text, plus the file it came from. A volume named `landing` holds the raw files |
| `<domain>_silver` | Cleaned and typed. Bad rows are kept apart with a reason |
| `<domain>_gold` | Business tables: ready for a dashboard or a model |
| `sandbox` | **Yours.** Create tables here. Never write to the other schemas |

Two domains: `retail` (one UK online shop, two years of invoices) and `healthcare` (invented patients, visits, diagnoses, drugs, claims).

## How to work

1. Open the folder of notebooks next to this file. Each domain has numbered SQL notebooks (bronze, silver, gold) that show exactly how each layer was built.
2. Read a notebook, then run queries against the tables it created. You do not need to re-run the notebooks: the tables already exist.
3. Each notebook ends with an exercise. Do them in order. Work in `sandbox`.
4. Use a SQL warehouse. If the first query is slow, the warehouse is starting.

## Rules of the lab

- Look before you trust: always check the row count of a table and the rows that were rejected.
- If a number looks wrong, find the layer where it went wrong. That is what the layers are for.
- `healthcare` has governance applied: some columns are masked and some rows are hidden from you. This is deliberate.
