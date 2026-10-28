# dbx-lakehouse: a medallion lakehouse with real data, ready to teach

One command builds a working lakehouse in a Databricks workspace: bronze, silver and gold tables in Unity Catalog, real open data already loaded, governance applied, and SQL notebooks with exercises for the learners.

It runs on **Databricks Free Edition, AWS and Azure**. It needs only a SQL warehouse and the Databricks CLI login. No cluster, no job, no Terraform.

**Audience:** data engineers and analysts learning the medallion pattern and Unity Catalog. Instructors who need a dataset that is already built.

## Launch

```bash
databricks auth login                                   # once. Free Edition: use the workspace URL from your account
labagent launch dbx-lakehouse --name lab1
```

That builds both domains. Useful options:

```bash
--param domains=retail                    # one domain only (retail | healthcare)
--param catalog=lakehouse                 # catalog to create (default lakehouse)
--param readers="account users"           # who may read silver and gold and use the sandbox. Empty: nobody
--param clinical_group=lakehouse_clinicians   # healthcare: group that sees unmasked identifiers
--param warehouse_id=<id>                 # default: a serverless or running warehouse, or a new 2X-Small if there is none
--param profile=<name>                    # a Databricks CLI profile other than the default
--param rebuild=yes                       # run every step again
--dry-run                                 # list the steps, touch nothing
```

Remove it again. This drops the schemas the pack created (including anything learners put in `sandbox`), the catalog if the pack created it, and the notebooks folder:

```bash
labagent destroy dbx-lakehouse --name lab1
```

## What you get

| Domain | Source | Licence | Rows |
|---|---|---|---|
| `retail` | [UCI Online Retail II](https://doi.org/10.24432/C5CG6D) (Chen, 2012), a UK online gift shop, Dec 2009 to Dec 2011 | CC BY 4.0 | 1,067,371 invoice lines |
| `healthcare` | [Synthea](https://github.com/synthetichealth/synthea) sample data: invented patients, visits, diagnoses, drugs, claims | Public domain (stated on the [Synthea DPG page](https://github.com/synthetichealth/synthea/wiki/DPG)) | about 90,000 rows in 13 tables, about 110 patients |

Each domain gets three schemas and a volume (`<domain>_bronze.landing`):

| Layer | Meaning | Retail | Healthcare |
|---|---|---|---|
| bronze | Raw, all text, with source file and load time | `invoice_lines_raw` | 13 `*_raw` tables |
| silver | Typed, cleaned, deduplicated. Bad rows kept apart with a reason | `invoice_lines`, `invoice_lines_rejected`, `customers`, `products` | 13 tables (`patients`, `encounters`, `conditions`, ...) |
| gold | Business tables | `daily_sales`, `monthly_revenue`, `customer_rfm`, `product_performance` | `patient_360`, `encounters_by_month_class`, `condition_prevalence`, `payer_performance`, `medication_utilization` |

Plus a `sandbox` schema where learners create their own tables, and a folder of numbered SQL notebooks (with exercises and a guide) in the instructor's workspace home.

The SQL notebooks are the single source of truth. The pack runs them cell by cell, and uploads the same files for learners, so what they read is exactly what built the tables.

### What each domain teaches

**Retail** (data engineering): messy real data. A quarter of the lines have no customer, cancellations are negative lines, some "products" are postage and fees, there are zero prices, and one December week is in the source twice. Silver has five written rules and a rejected table. The last three months are left in the volume for an incremental-load exercise with `COPY INTO`.

**Healthcare** (governance): the same flow, plus Unity Catalog controls applied last:
- tags on the columns that identify people
- column masks on `ssn`, `drivers`, `passport` and `address`
- a row filter that hides `snf` and `hospice` visits
- the raw schema and volume are not readable by learners
- anyone in the account group `lakehouse_clinicians` sees everything. Create the group in the account console and add the instructor. **Until you do, everyone sees masked data, including you.**

### Instructor notes

- Gold tables are built before the masks and the filter exist, on purpose. The healthcare notebook ends with the question: does governance on silver protect gold? It does not by itself. The fix is to build gold from a governed view, or to apply the same controls to gold.
- The retail `duplicate` rule removes exact copies. Some are the December overlap (true duplicates), some are a customer scanning an item twice (real). The exercise asks learners to argue for a different rule.
- `try_cast` hides bad values as NULL. The healthcare exercise asks learners to find the NULLs that should not be there.
- The healthcare sample is small (about 110 patients), so readmission counts are tiny. That is part of the exercise, not an error.

## Where it works, and how it adapts

| | Free Edition | AWS | Azure |
|---|---|---|---|
| Compute | Serverless starter warehouse | Any SQL warehouse (a 2X-Small is created if there is none) | Same |
| Catalog | Created if allowed, otherwise the current catalog is used | Same | Same |
| Sharing | `readers` defaults to `account users` | Same | Same |

If the workspace cannot create a catalog (a metastore with no storage root needs a storage credential and an external location first), the pack says so and builds in the **current catalog** instead. In that case it does not grant `USE CATALOG`, so check that learners already have it.

## Data

The data is downloaded from the `labagent-datasets` release `lakehouse-2026.10` and checked against a SHA-256 stored in the code (`labagent/packs/rag/sources/datasets.json`). A file whose hash differs is refused. Each domain's licence notice (the CC BY attribution) is uploaded to the landing volume with the data.

Offline, or to test a change to the data, unpack the snapshot into `<folder>/retail/` and `<folder>/healthcare/` and pass `--param data_dir=<folder>`.

To rebuild the snapshots from the sources (maintainers only):

```bash
pip install openpyxl
python scripts/build_lakehouse_data.py --uci online_retail_II.xlsx --synthea synthea_sample_data_csv_latest.zip --out dist-data
```

The build is reproducible byte for byte. The retail row count (1,067,371) matches the number UCI states.

## Safety

- Nothing is a secret here: the Databricks CLI holds the login and the pack never sees a token.
- The pack drops only schemas it recorded as its own. A schema that already exists and is not in the pack's records stops the launch.
- Names are validated before any SQL runs: catalog, groups and readers cannot carry quotes or backticks.
- A step that fails half way keeps what it already created in its records, so `destroy` can still remove it.

## Tested

- **Databricks Free Edition: built and destroyed for real (2026-10-28).** Both domains, all 40 tables, checks passed, the catalog was created, every optional statement (tags, masks, row filter, constraints) applied, and `destroy` left the workspace as it was found. Retail bronze 896,392 rows (the other 170,979 wait in `incoming/`), silver 853,865, rejected 42,527.
- **AWS and Azure: not yet run.** The pack uses the same SQL warehouse path, but the catalog step differs there: a metastore without a storage root cannot create a catalog, and the pack then falls back to the current catalog. That fallback is covered by mocked tests only.
- Not tested on a real workspace: sharing with `readers` (the live test ran with none), the `clinical_group` unmasking, and creating a warehouse when there is none.

## Known limits

- Cost: only the SQL warehouse costs money. A serverless 2X-Small runs a few minutes for the build. Free Edition has a daily fair-use compute limit.
- The pack does not create the account group, and cannot grant on groups that do not exist. A grant that fails is reported in the result, not hidden.
- Tags, constraints, column masks and the row filter are marked optional: if a workspace does not support one, the build continues and says which were skipped.
- Only two domains exist. More (NYC taxi trips, the built-in `samples.wanderbricks`) are candidates.
