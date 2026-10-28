# Retail guide

**Data:** UCI *Online Retail II* (Chen, D., 2012), CC BY 4.0. 1,067,371 invoice lines from a UK online gift shop, December 2009 to December 2011.
Cite it if you publish anything.

**What is hard about it (all real):**
- A quarter of the lines have no customer.
- Cancellations are separate lines with an invoice starting with `C` and negative quantities.
- Some "products" are not products: postage, bank charges, test codes.
- Zero and negative prices exist.
- 2010-12-01 to 2010-12-09 is in the source twice, so some lines are exact duplicates. Some other duplicates are real second scans.

| Layer | Tables |
|---|---|
| bronze | `invoice_lines_raw` (loaded from 22 monthly files; the last 3 months are waiting in the volume) |
| silver | `invoice_lines`, `invoice_lines_rejected`, `customers`, `products`, view `v_invoice_lines_checked` |
| gold | `daily_sales`, `monthly_revenue`, `customer_rfm`, `product_performance` |

**Exercises** are at the end of each notebook:
1. Incremental load: copy bronze into `sandbox`, load `incoming/` with `COPY INTO`, run it twice.
2. Audit the quality rules, and argue about the duplicate rule.
3. Make gold answer a new question, then rebuild with the new months and see who moved segment.

**Stretch:** build a Delta table of customers with `Champions` as a feature table, then compute the 30-day repeat purchase rate.
