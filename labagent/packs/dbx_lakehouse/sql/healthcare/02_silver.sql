-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Healthcare, step 2 of 3: silver
-- MAGIC
-- MAGIC **Silver is clean, typed and consistent.** What changes from bronze:
-- MAGIC - text becomes dates, timestamps, decimals and booleans (`try_cast`, so one bad value becomes NULL and not a failed job)
-- MAGIC - column names become readable (`Id` becomes `patient_id`)
-- MAGIC - the digits Synthea adds to names (`Maximina916`) are removed
-- MAGIC - repeated keys are removed
-- MAGIC
-- MAGIC The governance notebook (step 4) then masks identifiers in `patients` and hides sensitive care settings in `encounters`.

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.patients
COMMENT 'One row per patient. Holds synthetic direct identifiers (ssn, drivers, passport, address): they are masked for anyone outside the clinical group.'
AS
SELECT Id                                                      AS patient_id,
       try_cast(BIRTHDATE AS DATE)                             AS birth_date,
       try_cast(nullif(DEATHDATE, '') AS DATE)                 AS death_date,
       nullif(SSN, '')                                         AS ssn,
       nullif(DRIVERS, '')                                     AS drivers,
       nullif(PASSPORT, '')                                    AS passport,
       nullif(PREFIX, '')                                      AS prefix,
       regexp_replace(`FIRST`, '[0-9]+', '')                   AS first_name,
       regexp_replace(nullif(MIDDLE, ''), '[0-9]+', '')        AS middle_name,
       regexp_replace(`LAST`, '[0-9]+', '')                    AS last_name,
       MARITAL                                                 AS marital_status,
       RACE                                                    AS race,
       ETHNICITY                                               AS ethnicity,
       GENDER                                                  AS gender,
       ADDRESS                                                 AS address,
       CITY                                                    AS city,
       STATE                                                   AS state,
       COUNTY                                                  AS county,
       ZIP                                                     AS zip,
       try_cast(LAT AS DOUBLE)                                 AS lat,
       try_cast(LON AS DOUBLE)                                 AS lon,
       try_cast(HEALTHCARE_EXPENSES AS DECIMAL(14,2))          AS healthcare_expenses,
       try_cast(HEALTHCARE_COVERAGE AS DECIMAL(14,2))          AS healthcare_coverage,
       try_cast(INCOME AS INT)                                 AS income
FROM __CATALOG__.healthcare_bronze.patients_raw
QUALIFY row_number() OVER (PARTITION BY Id ORDER BY _ingested_at) = 1

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.encounters
COMMENT 'One row per visit or stay. encounter_class says the setting: ambulatory, wellness, outpatient, emergency, urgentcare, inpatient, virtual, home, snf, hospice.'
AS
WITH t AS (
  SELECT Id AS encounter_id, try_cast(`START` AS TIMESTAMP) AS start_ts, try_cast(`STOP` AS TIMESTAMP) AS stop_ts,
         PATIENT AS patient_id, ORGANIZATION AS organization_id, PROVIDER AS provider_id, PAYER AS payer_id,
         ENCOUNTERCLASS AS encounter_class, `CODE` AS code, `DESCRIPTION` AS description,
         try_cast(BASE_ENCOUNTER_COST AS DECIMAL(12,2)) AS base_cost,
         try_cast(TOTAL_CLAIM_COST AS DECIMAL(12,2))    AS total_claim_cost,
         try_cast(PAYER_COVERAGE AS DECIMAL(12,2))      AS payer_coverage,
         nullif(REASONCODE, '') AS reason_code, nullif(REASONDESCRIPTION, '') AS reason_description,
         _ingested_at
  FROM __CATALOG__.healthcare_bronze.encounters_raw
)
SELECT encounter_id, start_ts, stop_ts, timestampdiff(MINUTE, start_ts, stop_ts) AS duration_minutes,
       patient_id, organization_id, provider_id, payer_id, encounter_class, code, description,
       base_cost, total_claim_cost, payer_coverage, total_claim_cost - payer_coverage AS patient_paid,
       reason_code, reason_description
FROM t
QUALIFY row_number() OVER (PARTITION BY encounter_id ORDER BY _ingested_at) = 1

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.conditions
COMMENT 'Diagnoses. A condition with no stop date is still active (is_active).'
AS
SELECT PATIENT AS patient_id, ENCOUNTER AS encounter_id,
       try_cast(`START` AS DATE) AS start_date, try_cast(nullif(`STOP`, '') AS DATE) AS stop_date,
       nullif(`STOP`, '') IS NULL AS is_active,
       `SYSTEM` AS code_system, `CODE` AS code, `DESCRIPTION` AS description
FROM __CATALOG__.healthcare_bronze.conditions_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.medications
COMMENT 'Prescriptions. A medication with no stop time is still being taken.'
AS
SELECT PATIENT AS patient_id, ENCOUNTER AS encounter_id, PAYER AS payer_id,
       try_cast(`START` AS TIMESTAMP) AS start_ts, try_cast(nullif(`STOP`, '') AS TIMESTAMP) AS stop_ts,
       `CODE` AS code, `DESCRIPTION` AS description,
       try_cast(BASE_COST AS DECIMAL(12,2)) AS base_cost, try_cast(PAYER_COVERAGE AS DECIMAL(12,2)) AS payer_coverage,
       try_cast(DISPENSES AS INT) AS dispenses, try_cast(TOTALCOST AS DECIMAL(12,2)) AS total_cost,
       nullif(REASONDESCRIPTION, '') AS reason_description
FROM __CATALOG__.healthcare_bronze.medications_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.observations
COMMENT 'Measurements and survey answers (vital signs, lab results). value stays text, and value_numeric is set when it is a number.'
AS
SELECT PATIENT AS patient_id, ENCOUNTER AS encounter_id, try_cast(`DATE` AS TIMESTAMP) AS observed_ts,
       CATEGORY AS category, `CODE` AS code, `DESCRIPTION` AS description,
       `VALUE` AS value, try_cast(`VALUE` AS DOUBLE) AS value_numeric, nullif(UNITS, '') AS units, `TYPE` AS value_type
FROM __CATALOG__.healthcare_bronze.observations_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.procedures
COMMENT 'Procedures performed during an encounter, with the base cost.'
AS
SELECT PATIENT AS patient_id, ENCOUNTER AS encounter_id,
       try_cast(`START` AS TIMESTAMP) AS start_ts, try_cast(nullif(`STOP`, '') AS TIMESTAMP) AS stop_ts,
       `SYSTEM` AS code_system, `CODE` AS code, `DESCRIPTION` AS description,
       try_cast(BASE_COST AS DECIMAL(12,2)) AS base_cost, nullif(REASONDESCRIPTION, '') AS reason_description
FROM __CATALOG__.healthcare_bronze.procedures_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.immunizations
COMMENT 'Vaccines given.'
AS
SELECT PATIENT AS patient_id, ENCOUNTER AS encounter_id, try_cast(`DATE` AS TIMESTAMP) AS given_ts,
       `CODE` AS code, `DESCRIPTION` AS description, try_cast(BASE_COST AS DECIMAL(12,2)) AS base_cost
FROM __CATALOG__.healthcare_bronze.immunizations_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.allergies
COMMENT 'Recorded allergies and intolerances.'
AS
SELECT PATIENT AS patient_id, ENCOUNTER AS encounter_id,
       try_cast(`START` AS DATE) AS start_date, try_cast(nullif(`STOP`, '') AS DATE) AS stop_date,
       `CODE` AS code, `DESCRIPTION` AS description, `TYPE` AS allergy_type, CATEGORY AS category,
       nullif(DESCRIPTION1, '') AS reaction, nullif(SEVERITY1, '') AS severity
FROM __CATALOG__.healthcare_bronze.allergies_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.careplans
COMMENT 'Care plans. A plan with no stop date is still in force.'
AS
SELECT Id AS careplan_id, PATIENT AS patient_id, ENCOUNTER AS encounter_id,
       try_cast(`START` AS DATE) AS start_date, try_cast(nullif(`STOP`, '') AS DATE) AS stop_date,
       `CODE` AS code, `DESCRIPTION` AS description, nullif(REASONDESCRIPTION, '') AS reason_description
FROM __CATALOG__.healthcare_bronze.careplans_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.organizations
COMMENT 'Hospitals and clinics.'
AS
SELECT Id AS organization_id, NAME AS name, ADDRESS AS address, CITY AS city, STATE AS state, ZIP AS zip,
       try_cast(LAT AS DOUBLE) AS lat, try_cast(LON AS DOUBLE) AS lon,
       try_cast(REVENUE AS DECIMAL(16,2)) AS revenue, try_cast(UTILIZATION AS INT) AS utilization
FROM __CATALOG__.healthcare_bronze.organizations_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.providers
COMMENT 'Clinicians. Synthea adds digits to names, and they are removed here.'
AS
SELECT Id AS provider_id, ORGANIZATION AS organization_id, regexp_replace(NAME, '[0-9]+', '') AS name,
       GENDER AS gender, SPECIALITY AS speciality, try_cast(ENCOUNTERS AS INT) AS encounters, try_cast(PROCEDURES AS INT) AS procedures
FROM __CATALOG__.healthcare_bronze.providers_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.payers
COMMENT 'Insurers and programmes (Medicare, Medicaid, private plans, no insurance).'
AS
SELECT Id AS payer_id, NAME AS name, OWNERSHIP AS ownership,
       try_cast(AMOUNT_COVERED AS DECIMAL(16,2)) AS amount_covered, try_cast(AMOUNT_UNCOVERED AS DECIMAL(16,2)) AS amount_uncovered,
       try_cast(UNIQUE_CUSTOMERS AS INT) AS unique_customers
FROM __CATALOG__.healthcare_bronze.payers_raw

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_silver.claims
COMMENT 'Claims, reduced to the columns analysts use. The raw table has about 40.'
AS
SELECT Id AS claim_id, PATIENTID AS patient_id, PROVIDERID AS provider_id,
       try_cast(SERVICEDATE AS TIMESTAMP) AS service_ts, nullif(DIAGNOSIS1, '') AS primary_diagnosis_code,
       STATUS1 AS status, try_cast(OUTSTANDING1 AS DECIMAL(12,2)) AS outstanding
FROM __CATALOG__.healthcare_bronze.claims_raw

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Exercise 2: check silver against bronze
-- MAGIC
-- MAGIC 1. Row counts: does every silver table have as many rows as its raw table? Which ones are allowed to differ, and why?
-- MAGIC 2. How many `patients` have a `death_date`? Does `encounters` contain a visit after a patient's death? What would you do about it?
-- MAGIC 3. `try_cast` turns a bad value into NULL and hides the problem. Write a query that counts values that became NULL by accident in `observations.value_numeric`.
-- MAGIC    (Many are legitimately text, such as survey answers: you need a rule that separates the two.)
