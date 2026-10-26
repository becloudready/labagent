# Kaggle dataset description (draft, not published)

**Title:** Synthetic AML Transactions with Ground Truth (Canada and US)

**Subtitle:** Customers, accounts, transfers, alerts and an answer key for a made-up credit union. For RAG, triage and Copilot practice.

**Description**

This dataset is synthetic. Maple Ridge Credit Union does not exist, and every name and number is invented.

It has seven tables: customers, accounts, transactions, alerts, ground truth, customer profiles and name-screening results. The small set has 300 customers, about 42,000 transfers and 72 alerts. Each alert has a decision (escalate, monitor or dismiss), a pattern and a plain-English reason.

The laundering cases use the 8 patterns from the IBM paper "Realistic Synthetic Financial Transactions for Anti-Money Laundering Models" (fan-out, fan-in, gather-scatter, scatter-gather, cycle, random, bipartite, stack), plus laundering disguised as payroll or supplier invoices. There are also look-alikes that deserve a closer look and ordinary events that trigger a rule for a harmless reason.

**Labels:** they come from how the data was built, not from an investigator.

**Use it to:**
- test a retrieval-augmented generation app over policies and regulations,
- practise alert triage and report narrative writing,
- teach Microsoft 365 Copilot on Excel and Word files.

**Generate more:** `pip install labagent`, then `labagent launch aml --name demo`. Same seed, same data. See the lab guide in the labagent repository, docs/aml.md.

**Licence:** CC BY 4.0. Credit: BeCloudReady labagent. Patterns after Altman et al. (2023), arXiv 2306.16424, and IBM AMLSim (Apache-2.0). No IBM data is included.

**Tags:** finance, banking, anti-money laundering, synthetic data, tabular data, classification

**Link back:** https://github.com/becloudready/labagent
