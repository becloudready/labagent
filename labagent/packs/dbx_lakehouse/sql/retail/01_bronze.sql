-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Retail, step 1 of 3: bronze
-- MAGIC
-- MAGIC **Bronze keeps the data exactly as it arrived.** Every column is text, nothing is fixed, nothing is dropped. If silver has a bug, bronze lets you rebuild it.
-- MAGIC
-- MAGIC Source: UCI *Online Retail II* (Chen, D., 2012, CC BY 4.0), one UK online gift shop, December 2009 to December 2011.
-- MAGIC The raw files are in the volume `__CATALOG__.retail_bronze.landing`, one gzip CSV per invoice month.
-- MAGIC
-- MAGIC `COPY INTO` remembers which files it has loaded, so running it again loads only new files and never duplicates rows.
-- MAGIC
-- MAGIC The pack loaded the months in `initial/`. The last three months are in `incoming/`: loading them is **exercise 1**.

-- COMMAND ----------

CREATE TABLE IF NOT EXISTS __CATALOG__.retail_bronze.invoice_lines_raw (
  invoice       STRING COMMENT 'Invoice number as delivered. A leading C marks a cancellation',
  stock_code    STRING COMMENT 'Product code as delivered. Not every code is a product (postage, fees, tests)',
  description   STRING,
  quantity      STRING COMMENT 'Text on purpose. Negative for returns',
  invoice_date  STRING COMMENT 'Text on purpose: silver turns it into a timestamp',
  price         STRING COMMENT 'Unit price in GBP, as text',
  customer_id   STRING COMMENT 'Empty for about a quarter of the lines',
  country       STRING,
  _source_file  STRING COMMENT 'Which file this line came from',
  _ingested_at  TIMESTAMP COMMENT 'When it was loaded'
)
COMMENT 'Raw invoice lines, one row per line of each file, all text. Source: UCI Online Retail II (CC BY 4.0).'

-- COMMAND ----------

COPY INTO __CATALOG__.retail_bronze.invoice_lines_raw
FROM (
  SELECT Invoice AS invoice, StockCode AS stock_code, Description AS description, Quantity AS quantity,
         InvoiceDate AS invoice_date, Price AS price, `Customer ID` AS customer_id, Country AS country,
         _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
  FROM '/Volumes/__CATALOG__/retail_bronze/landing/initial'
)
FILEFORMAT = CSV
FORMAT_OPTIONS ('header' = 'true', 'inferSchema' = 'false')

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Exercise 1: load the next three months
-- MAGIC
-- MAGIC Do not touch this table. Make your own copy in the `sandbox` schema, then load `incoming/` into it:
-- MAGIC
-- MAGIC ```sql
-- MAGIC CREATE TABLE __CATALOG__.sandbox.my_invoice_lines_raw DEEP CLONE __CATALOG__.retail_bronze.invoice_lines_raw;
-- MAGIC -- then COPY INTO your table from '/Volumes/__CATALOG__/retail_bronze/landing/incoming' (same SELECT as above)
-- MAGIC ```
-- MAGIC
-- MAGIC Run the COPY INTO twice. What does the second run report, and why?
