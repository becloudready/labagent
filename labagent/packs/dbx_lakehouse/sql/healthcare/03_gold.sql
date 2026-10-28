-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Healthcare, step 3 of 3: gold
-- MAGIC
-- MAGIC **Gold answers business questions, and holds no direct identifiers.**
-- MAGIC
-- MAGIC | Table | Question it answers |
-- MAGIC |---|---|
-- MAGIC | `patient_360` | One row per patient: age, visits, active conditions and medications, lifetime cost, risk band |
-- MAGIC | `encounters_by_month_class` | How do visits and cost move by month and care setting? |
-- MAGIC | `condition_prevalence` | Which conditions are most common, and at what age do they start? |
-- MAGIC | `payer_performance` | How much does each payer cover, and how much is left to the patient? |
-- MAGIC | `medication_utilization` | Which drugs are prescribed most, and what do they cost? |

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_gold.patient_360
COMMENT 'One row per patient with age, activity, active conditions and medications, lifetime cost and a simple risk band. No names or identifiers.'
AS
WITH enc AS (
  SELECT patient_id, count(*) AS encounters, max(start_ts) AS last_encounter, sum(total_claim_cost) AS lifetime_cost
  FROM __CATALOG__.healthcare_silver.encounters GROUP BY patient_id
),
cond AS (
  SELECT patient_id, count(DISTINCT description) AS active_conditions
  FROM __CATALOG__.healthcare_silver.conditions WHERE is_active GROUP BY patient_id
),
med AS (
  SELECT patient_id, count(DISTINCT description) AS active_medications
  FROM __CATALOG__.healthcare_silver.medications WHERE stop_ts IS NULL GROUP BY patient_id
),
base AS (
  SELECT p.patient_id, p.gender, p.race, p.city, p.death_date IS NOT NULL AS deceased,
         CAST(floor(months_between(coalesce(p.death_date, current_date()), p.birth_date) / 12) AS INT) AS age,
         coalesce(e.encounters, 0) AS encounters, e.last_encounter, coalesce(e.lifetime_cost, 0) AS lifetime_cost,
         coalesce(c.active_conditions, 0) AS active_conditions, coalesce(m.active_medications, 0) AS active_medications
  FROM __CATALOG__.healthcare_silver.patients p
  LEFT JOIN enc e USING (patient_id)
  LEFT JOIN cond c USING (patient_id)
  LEFT JOIN med m USING (patient_id)
)
SELECT *,
       CASE WHEN active_conditions >= 8 OR age >= 75 THEN 'high'
            WHEN active_conditions >= 4 OR age >= 60 THEN 'medium'
            ELSE 'low' END AS risk_band
FROM base

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_gold.encounters_by_month_class
COMMENT 'Visits, cost, insurer cover and average length per month and care setting.'
AS
SELECT CAST(date_trunc('MONTH', start_ts) AS DATE) AS month,
       encounter_class,
       count(*)                                    AS encounters,
       sum(total_claim_cost)                       AS total_cost,
       sum(payer_coverage)                         AS payer_coverage,
       round(avg(duration_minutes), 1)             AS avg_duration_minutes
FROM __CATALOG__.healthcare_silver.encounters
GROUP BY date_trunc('MONTH', start_ts), encounter_class

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_gold.condition_prevalence
COMMENT 'How many patients have had each condition, as a share of all patients, and the average age when it started.'
AS
WITH totals AS (SELECT count(*) AS n FROM __CATALOG__.healthcare_silver.patients)
SELECT c.description AS condition,
       count(DISTINCT c.patient_id)                                                        AS patients,
       round(100 * count(DISTINCT c.patient_id) / max(t.n), 1)                             AS prevalence_pct,
       round(avg(months_between(c.start_date, p.birth_date) / 12), 1)                      AS avg_age_at_onset,
       sum(CASE WHEN c.is_active THEN 1 ELSE 0 END)                                        AS active_cases
FROM __CATALOG__.healthcare_silver.conditions c
JOIN __CATALOG__.healthcare_silver.patients p USING (patient_id)
CROSS JOIN totals t
GROUP BY c.description

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_gold.payer_performance
COMMENT 'Encounters, cost, what the payer covered and what the patient paid, per payer.'
AS
SELECT coalesce(py.name, 'Unknown') AS payer,
       count(*)                                              AS encounters,
       sum(e.total_claim_cost)                               AS total_cost,
       sum(e.payer_coverage)                                 AS payer_coverage,
       sum(e.patient_paid)                                   AS patient_paid,
       round(100 * sum(e.payer_coverage) / nullif(sum(e.total_claim_cost), 0), 1) AS coverage_pct
FROM __CATALOG__.healthcare_silver.encounters e
LEFT JOIN __CATALOG__.healthcare_silver.payers py USING (payer_id)
GROUP BY coalesce(py.name, 'Unknown')

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_gold.medication_utilization
COMMENT 'Prescriptions, patients, dispenses and cost per drug.'
AS
SELECT description AS medication,
       count(*)                       AS prescriptions,
       count(DISTINCT patient_id)     AS patients,
       sum(dispenses)                 AS dispenses,
       sum(total_cost)                AS total_cost
FROM __CATALOG__.healthcare_silver.medications
GROUP BY description

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_gold.patient_360 SET TAGS ('layer' = 'gold', 'domain' = 'healthcare')

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_gold.patient_360 ALTER COLUMN risk_band COMMENT 'high: 8 or more active conditions or age 75+. medium: 4 or more conditions or age 60+. Teaching rule, not clinical advice'

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Exercise 3: build your own gold table
-- MAGIC
-- MAGIC In the `sandbox` schema, build `readmissions`: patients with an `inpatient` encounter that began within 30 days of the end of a previous
-- MAGIC `inpatient` encounter. Then say how many you found and why the number is small (hint: how many patients does this data have?).
-- MAGIC
-- MAGIC The `snf` and `hospice` encounters are hidden from you in silver after step 4, but `encounters_by_month_class` was built before that.
-- MAGIC Is that a leak? Explain what you would change in the pipeline.
