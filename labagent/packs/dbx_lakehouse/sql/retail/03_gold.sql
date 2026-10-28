-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Retail, step 3 of 3: gold
-- MAGIC
-- MAGIC **Gold answers business questions.** Four small tables built from silver, ready for a dashboard or a model:
-- MAGIC
-- MAGIC | Table | Question it answers |
-- MAGIC |---|---|
-- MAGIC | `daily_sales` | How much did we sell, return and net, per day and country? |
-- MAGIC | `monthly_revenue` | How is revenue trending month on month? |
-- MAGIC | `customer_rfm` | Who are the best and the lapsing customers? (recency, frequency, monetary, scored 1 to 5) |
-- MAGIC | `product_performance` | Which products earn the most, and which are returned the most? |

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_gold.daily_sales
COMMENT 'Sales, returns and net revenue per day and country, in GBP. Source: retail_silver.invoice_lines.'
AS
SELECT invoice_date AS sale_date,
       country,
       count(DISTINCT CASE WHEN NOT is_cancellation THEN invoice_no END)  AS orders,
       sum(CASE WHEN NOT is_cancellation THEN quantity ELSE 0 END)        AS units_sold,
       sum(CASE WHEN NOT is_cancellation THEN line_total ELSE 0 END)      AS gross_revenue,
       -sum(CASE WHEN is_cancellation THEN line_total ELSE 0 END)         AS returned_revenue,
       sum(line_total)                                                    AS net_revenue
FROM __CATALOG__.retail_silver.invoice_lines
GROUP BY invoice_date, country

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_gold.monthly_revenue
COMMENT 'Net revenue, orders and known customers per month, with month on month growth.'
AS
WITH m AS (
  SELECT date_trunc('MONTH', invoice_date) AS month,
         sum(line_total)                   AS net_revenue,
         count(DISTINCT CASE WHEN NOT is_cancellation THEN invoice_no END) AS orders,
         count(DISTINCT customer_id)       AS customers
  FROM __CATALOG__.retail_silver.invoice_lines
  GROUP BY date_trunc('MONTH', invoice_date)
)
SELECT CAST(month AS DATE) AS month, net_revenue, orders, customers,
       round(100 * (net_revenue - lag(net_revenue) OVER (ORDER BY month)) / lag(net_revenue) OVER (ORDER BY month), 1) AS mom_growth_pct
FROM m

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_gold.customer_rfm
COMMENT 'One row per known customer: days since last purchase, number of orders, net spend, and 1 to 5 scores for each. 5 is best.'
AS
WITH as_of AS (
  SELECT date_add(max(invoice_date), 1) AS d FROM __CATALOG__.retail_silver.invoice_lines
),
per_customer AS (
  SELECT customer_id,
         max(CASE WHEN NOT is_cancellation THEN invoice_date END)                   AS last_purchase_date,
         count(DISTINCT CASE WHEN NOT is_cancellation THEN invoice_no END)          AS frequency,
         sum(line_total)                                                            AS monetary
  FROM __CATALOG__.retail_silver.invoice_lines
  WHERE customer_id IS NOT NULL
  GROUP BY customer_id
  HAVING count(DISTINCT CASE WHEN NOT is_cancellation THEN invoice_no END) > 0
),
scored AS (
  SELECT p.customer_id, p.last_purchase_date, p.frequency, p.monetary,
         datediff(a.d, p.last_purchase_date)                                        AS recency_days,
         ntile(5) OVER (ORDER BY datediff(a.d, p.last_purchase_date) DESC)          AS r_score,
         ntile(5) OVER (ORDER BY p.frequency)                                       AS f_score,
         ntile(5) OVER (ORDER BY p.monetary)                                        AS m_score
  FROM per_customer p CROSS JOIN as_of a
)
SELECT customer_id, last_purchase_date, recency_days, frequency, monetary, r_score, f_score, m_score,
       CASE
         WHEN r_score >= 4 AND f_score >= 4 THEN 'Champions'
         WHEN r_score >= 3 AND f_score >= 3 THEN 'Loyal'
         WHEN r_score >= 4                  THEN 'Recent'
         WHEN r_score <= 2 AND f_score >= 4 THEN 'At risk'
         WHEN r_score <= 2                  THEN 'Lost'
         ELSE 'Needs attention'
       END AS segment
FROM scored

-- COMMAND ----------

CREATE OR REPLACE TABLE __CATALOG__.retail_gold.product_performance
COMMENT 'Revenue, units, orders and return rate per product, ranked by gross revenue.'
AS
WITH agg AS (
  SELECT stock_code,
         sum(CASE WHEN NOT is_cancellation THEN quantity ELSE 0 END)       AS units_sold,
         -sum(CASE WHEN is_cancellation THEN quantity ELSE 0 END)          AS units_returned,
         sum(CASE WHEN NOT is_cancellation THEN line_total ELSE 0 END)     AS gross_revenue,
         count(DISTINCT CASE WHEN NOT is_cancellation THEN invoice_no END) AS orders
  FROM __CATALOG__.retail_silver.invoice_lines
  GROUP BY stock_code
)
SELECT a.stock_code, p.description, a.units_sold, a.units_returned, a.gross_revenue, a.orders,
       round(100 * a.units_returned / nullif(a.units_sold, 0), 1)          AS return_rate_pct,
       rank() OVER (ORDER BY a.gross_revenue DESC)                         AS revenue_rank
FROM agg a JOIN __CATALOG__.retail_silver.products p USING (stock_code)

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.retail_gold.customer_rfm ALTER COLUMN monetary COMMENT 'Net spend in GBP: purchases minus returns'

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.retail_gold.customer_rfm ALTER COLUMN recency_days COMMENT 'Days between the last purchase and the day after the last invoice in the data'

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.retail_gold.customer_rfm SET TAGS ('layer' = 'gold', 'domain' = 'retail')

-- COMMAND ----------

-- @optional
ALTER TABLE __CATALOG__.retail_gold.daily_sales SET TAGS ('layer' = 'gold', 'domain' = 'retail')

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Exercise 3: make gold answer a new question
-- MAGIC
-- MAGIC 1. Which segment of `customer_rfm` earns the most, and what share of customers is it?
-- MAGIC 2. Add a gold table `country_summary` (revenue, customers, average order value) in your `sandbox` schema.
-- MAGIC 3. Load the three `incoming/` months (exercise 1), rebuild silver and gold from your own bronze copy, and compare `monthly_revenue`.
-- MAGIC    Which customers moved from `Champions` to `At risk`? What does that tell you about when RFM is computed?
