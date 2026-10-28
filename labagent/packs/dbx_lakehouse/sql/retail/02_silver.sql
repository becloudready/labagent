-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Retail, step 2 of 3: silver
-- MAGIC
-- MAGIC **Silver is clean, typed and trustworthy.** Each bronze line gets a type, and the first rule it breaks (if any). Lines that pass go to
-- MAGIC `invoice_lines`. Lines that fail go to `invoice_lines_rejected` with the reason, so nothing disappears silently.
-- MAGIC
-- MAGIC The rules, in order:
-- MAGIC 1. `unparseable`: quantity, date or price is not a valid number or date
-- MAGIC 2. `not_a_product`: the stock code does not start with five digits (POST, M, BANK CHARGES, DOT, test codes)
-- MAGIC 3. `bad_price`: the price is zero or negative
-- MAGIC 4. `bad_quantity`: a cancellation (invoice starts with C) must have a negative quantity, a sale a positive one
-- MAGIC 5. `duplicate`: the line is an exact copy of an earlier line

-- COMMAND ----------

CREATE OR REPLACE VIEW __CATALOG__.retail_silver.v_invoice_lines_checked
COMMENT 'Every bronze line with typed columns and the first rule it breaks. reject_reason is NULL when the line is good.'
AS
WITH typed AS (
  SELECT
    trim(invoice)                                                                 AS invoice_no,
    upper(trim(stock_code))                                                       AS stock_code,
    nullif(trim(description), '')                                                 AS description,
    try_cast(quantity AS INT)                                                     AS quantity,
    try_cast(invoice_date AS TIMESTAMP)                                           AS invoice_ts,
    try_cast(price AS DECIMAL(10,2))                                              AS unit_price,
    try_cast(try_cast(nullif(trim(customer_id), '') AS DOUBLE) AS BIGINT)         AS customer_id,
    trim(country)                                                                 AS country,
    _source_file,
    row_number() OVER (PARTITION BY invoice, stock_code, description, quantity, invoice_date, price, customer_id, country
                       ORDER BY _source_file)                                     AS copy_no
  FROM __CATALOG__.retail_bronze.invoice_lines_raw
)
SELECT
  *,
  startswith(invoice_no, 'C') AS is_cancellation,
  CASE
    WHEN quantity IS NULL OR invoice_ts IS NULL OR unit_price IS NULL OR invoice_no = '' THEN 'unparseable'
    WHEN NOT (stock_code RLIKE '^[0-9]{5}') THEN 'not_a_product'
    WHEN unit_price <= 0 THEN 'bad_price'
    WHEN (startswith(invoice_no, 'C') AND quantity >= 0) OR (NOT startswith(invoice_no, 'C') AND quantity <= 0) THEN 'bad_quantity'
    WHEN copy_no > 1 THEN 'duplicate'
  END AS reject_reason
FROM typed

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_silver.invoice_lines
COMMENT 'Clean invoice lines: typed, checked, duplicates removed. Sales and returns, one row per line.'
AS
SELECT invoice_no, stock_code, description, quantity, invoice_ts, CAST(invoice_ts AS DATE) AS invoice_date,
       unit_price, CAST(quantity * unit_price AS DECIMAL(12,2)) AS line_total,
       customer_id, country, is_cancellation, _source_file
FROM __CATALOG__.retail_silver.v_invoice_lines_checked
WHERE reject_reason IS NULL

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_silver.invoice_lines_rejected
COMMENT 'Bronze lines that failed a rule, with the reason. Check this table before trusting silver.'
AS
SELECT invoice_no, stock_code, description, quantity, invoice_ts, unit_price, customer_id, country, reject_reason, _source_file
FROM __CATALOG__.retail_silver.v_invoice_lines_checked
WHERE reject_reason IS NOT NULL

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_silver.customers
COMMENT 'One row per known customer. About a quarter of the lines have no customer and are not here.'
AS
SELECT customer_id,
       max_by(country, invoice_ts) AS country,
       min(invoice_date)           AS first_seen,
       max(invoice_date)           AS last_seen,
       count(DISTINCT invoice_no)  AS invoices
FROM __CATALOG__.retail_silver.invoice_lines
WHERE customer_id IS NOT NULL
GROUP BY customer_id

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_silver.products
COMMENT 'One row per product code. The description is the most recent one: the source spells the same product several ways.'
AS
SELECT stock_code,
       max_by(description, invoice_ts) AS description,
       count(DISTINCT description)     AS description_variants,
       min(unit_price)                 AS min_price,
       max(unit_price)                 AS max_price
FROM __CATALOG__.retail_silver.invoice_lines
GROUP BY stock_code

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.retail_silver.invoice_lines ADD CONSTRAINT price_is_positive CHECK (unit_price > 0)

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.retail_silver.invoice_lines ADD CONSTRAINT quantity_is_not_zero CHECK (quantity <> 0)

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Exercise 2: audit the rules
-- MAGIC
-- MAGIC 1. How many lines fail each rule? (`GROUP BY reject_reason` on `invoice_lines_rejected`)
-- MAGIC 2. The `duplicate` rule removes exact copies. Some of those are not errors: a customer can scan the same item twice on one invoice.
-- MAGIC    Find invoices where a removed duplicate is probably a real second item. Would you keep the rule? What would you change?
-- MAGIC 3. Try to insert a row with a negative price into `invoice_lines`. Which constraint stops you?
