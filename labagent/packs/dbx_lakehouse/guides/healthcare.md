# Healthcare guide

**Data:** Synthea sample data. Every patient is invented (the Synthea project states its synthetic data is public domain), but the structure
follows real electronic health records: patients, encounters, conditions, medications, observations, procedures, immunizations, allergies, care plans, organizations, providers, payers and claims.
About 110 patients and 90,000 rows. It is small on purpose: it runs fast and every row can be inspected.

**What this lab teaches:** the same medallion flow as retail, plus **Unity Catalog governance**.
- `healthcare_bronze` holds identifiers in clear text. Learners cannot read it, as in a real raw zone.
- `healthcare_silver.patients` has tags on identifying columns and **masks** on `ssn`, `drivers`, `passport` and `address`.
- `healthcare_silver.encounters` has a **row filter** that hides `snf` and `hospice` visits.
- Anyone in the account group `__CLINICAL_GROUP__` sees everything. Ask the instructor to add you.

| Layer | Tables |
|---|---|
| bronze | 13 `*_raw` tables, one per file |
| silver | `patients`, `encounters`, `conditions`, `medications`, `observations`, `procedures`, `immunizations`, `allergies`, `careplans`, `organizations`, `providers`, `payers`, `claims` |
| gold | `patient_360`, `encounters_by_month_class`, `condition_prevalence`, `payer_performance`, `medication_utilization` |

**Exercises** are at the end of each notebook: read the raw layer, check silver against bronze, build a readmission table, prove the controls work.

**Think about:** gold tables were built before the filter and mask existed. Does governance on silver protect gold? (It does not, by itself.)
