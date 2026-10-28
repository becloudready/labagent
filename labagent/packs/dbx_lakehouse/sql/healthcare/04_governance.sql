-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Healthcare, step 4: governance with Unity Catalog
-- MAGIC
-- MAGIC Governance is applied **last** on purpose: the pack needs to read the raw values to build silver and gold.
-- MAGIC
-- MAGIC What this notebook sets up:
-- MAGIC 1. **Tags** on the columns that identify people, so they can be found and audited.
-- MAGIC 2. **Column masks** on `patients`: everyone outside the group `__CLINICAL_GROUP__` sees `ssn`, `drivers`, `passport` and `address` masked.
-- MAGIC 3. **A row filter** on `encounters`: everyone outside that group does not see `snf` and `hospice` visits (sensitive care settings).
-- MAGIC
-- MAGIC Masks and filters are checked **when a query runs**, by looking at who runs it. They never change the stored data.
-- MAGIC The group is an account group: an account admin creates it in the account console and adds the instructor.
-- MAGIC Until then, **everyone sees masked data, including the person who built it**.

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN ssn SET TAGS ('pii' = 'ssn')

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN drivers SET TAGS ('pii' = 'government_id')

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN passport SET TAGS ('pii' = 'government_id')

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN address SET TAGS ('pii' = 'address')

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN birth_date SET TAGS ('pii' = 'date_of_birth')

-- COMMAND ----------

CREATE OR REPLACE FUNCTION __CATALOG__.healthcare_silver.mask_ssn(ssn STRING)
RETURNS STRING
COMMENT 'Shows only the last four digits unless the caller is in the clinical group'
RETURN CASE WHEN is_account_group_member('__CLINICAL_GROUP__') THEN ssn ELSE concat('***-**-', right(ssn, 4)) END

-- COMMAND ----------

CREATE OR REPLACE FUNCTION __CATALOG__.healthcare_silver.mask_all(value STRING)
RETURNS STRING
COMMENT 'Hides the whole value unless the caller is in the clinical group'
RETURN CASE WHEN is_account_group_member('__CLINICAL_GROUP__') THEN value ELSE '*** masked ***' END

-- COMMAND ----------

CREATE OR REPLACE FUNCTION __CATALOG__.healthcare_silver.rf_hide_sensitive_settings(encounter_class STRING)
RETURNS BOOLEAN
COMMENT 'TRUE keeps the row. Hospice and skilled nursing visits are visible only to the clinical group'
RETURN is_account_group_member('__CLINICAL_GROUP__') OR encounter_class NOT IN ('hospice', 'snf')

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN ssn SET MASK __CATALOG__.healthcare_silver.mask_ssn

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN drivers SET MASK __CATALOG__.healthcare_silver.mask_all

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN passport SET MASK __CATALOG__.healthcare_silver.mask_all

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.patients ALTER COLUMN address SET MASK __CATALOG__.healthcare_silver.mask_all

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.healthcare_silver.encounters SET ROW FILTER __CATALOG__.healthcare_silver.rf_hide_sensitive_settings ON (encounter_class)

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Exercise 4: prove the controls work
-- MAGIC
-- MAGIC 1. `SELECT ssn, drivers, address FROM __CATALOG__.healthcare_silver.patients LIMIT 5`. What do you see? Why?
-- MAGIC 2. Count `encounters` by `encounter_class`. Compare with `healthcare_bronze.encounters_raw`. Which classes are missing?
-- MAGIC 3. Ask the instructor to add you to `__CLINICAL_GROUP__`, then repeat. What changed, and what did not (the stored files)?
-- MAGIC 4. Look at the column tags: `SELECT * FROM __CATALOG__.information_schema.column_tags WHERE schema_name = 'healthcare_silver'`.
-- MAGIC 5. Gold tables were built before the filter. Does `encounters_by_month_class` still count hospice visits? Is that a governance gap? Fix it.
