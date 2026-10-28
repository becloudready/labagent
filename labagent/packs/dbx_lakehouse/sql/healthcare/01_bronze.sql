-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Healthcare, step 1 of 3: bronze
-- MAGIC
-- MAGIC **Bronze keeps the data exactly as it arrived.** One table per file, every column text, plus where it came from and when it was loaded.
-- MAGIC
-- MAGIC Source: Synthea sample data. Every patient is invented, but the shape is real: patients, encounters, conditions, medications,
-- MAGIC observations, procedures, immunizations, allergies, care plans, organizations, providers, payers and claims.
-- MAGIC
-- MAGIC **Bronze holds social security numbers, driver's licence and passport numbers in clear text.** That is why the pack gives learners no access to this schema.
-- MAGIC Raw zones are restricted. Silver masks these columns, and gold has none of them.

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.patients_raw
COMMENT 'Synthea patients.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_patients.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.encounters_raw
COMMENT 'Synthea encounters.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_encounters.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.conditions_raw
COMMENT 'Synthea conditions.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_conditions.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.medications_raw
COMMENT 'Synthea medications.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_medications.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.observations_raw
COMMENT 'Synthea observations.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_observations.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.procedures_raw
COMMENT 'Synthea procedures.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_procedures.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.immunizations_raw
COMMENT 'Synthea immunizations.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_immunizations.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.allergies_raw
COMMENT 'Synthea allergies.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_allergies.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.careplans_raw
COMMENT 'Synthea careplans.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_careplans.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.organizations_raw
COMMENT 'Synthea organizations.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_organizations.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.providers_raw
COMMENT 'Synthea providers.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_providers.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.payers_raw
COMMENT 'Synthea payers.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_payers.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.healthcare_bronze.claims_raw
COMMENT 'Synthea claims.csv, every column text, exactly as delivered'
AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/__CATALOG__/healthcare_bronze/landing/healthcare_claims.csv', format => 'csv', header => true, inferSchema => false)

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Exercise 1: read the raw layer
-- MAGIC
-- MAGIC 1. Which columns hold information that identifies a person? List them for `patients_raw`. Which are risky on their own, and which only in combination?
-- MAGIC 2. Every raw table has a `_rescued_data` column. When would it contain something? Try loading a CSV with an extra column to find out.
-- MAGIC 3. Why is every column text here, when `BIRTHDATE` is plainly a date?
