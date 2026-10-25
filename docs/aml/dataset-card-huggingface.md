---
license: cc-by-4.0
language:
- en
pretty_name: Synthetic AML Transactions with Ground Truth (Canada and US)
size_categories:
- 10K<n<100K
task_categories:
- tabular-classification
- question-answering
tags:
- finance
- anti-money-laundering
- aml
- synthetic
- transaction-monitoring
- fintrac
- bank-secrecy-act
- rag
---

# Synthetic AML Transactions with Ground Truth (Canada and US)

Draft dataset card. The release is not published yet. Replace the placeholders marked `TODO` after you publish it.

A synthetic anti-money laundering dataset for a made-up credit union, Maple Ridge Credit Union. It has customers, accounts, transfers, alerts, a short profile for each customer, name-screening results, and an answer key for every alert. Policies, case files and exercises are built from the same data by the [labagent](https://github.com/becloudready/labagent) `aml` pack.

## What is in it

| File | Rows (small set) | Holds |
|---|---|---|
| `customers` | 300 | Synthetic name, person or business, occupation or industry, country, risk rating, expected monthly activity, onboarding date |
| `accounts` | 429 | Account, owner, type, open date, branch |
| `transactions` | about 42,000 | Time, from, to, amount, currency, channel (wire, cash, e-transfer, card, crypto off-ramp), description |
| `alerts` | 72 | Rule triggered, account, date, amount, risk score |
| `ground_truth` | 72 | Label (escalate, monitor, dismiss), pattern, accounts and transfers involved, reason |
| `kyc_profiles` | 300 | One short text profile per customer |
| `sanctions_hits` | 6 | Customer names seeded to match 12 OFAC SDN entity names, tagged TEST DATA |

A `medium` set has 2,000 customers, about 281,000 transfers and 552 alerts. Files are CSV, and Parquet when available. Generated with seed 42 unless the release says otherwise.

## How it was made

A seeded Python generator creates normal activity first (payroll, rent, card spending, business sales, supplier payments). It then adds labelled cases:

- the 8 laundering patterns from Altman et al. (2023), arXiv 2306.16424: fan-out, fan-in, gather-scatter, scatter-gather, cycle, random, bipartite and stack;
- laundering disguised as payroll or supplier invoices, as described in the same paper;
- name matches to OFAC SDN entity names, exact and slightly misspelled;
- look-alikes that deserve a closer look (monitor), such as many small gifts or a loan and its repayment;
- ordinary events that trigger a rule for a harmless reason (dismiss), such as a house sale.

The patterns follow IBM's [AMLSim](https://github.com/IBM/AMLSim) (Apache-2.0) and the paper. No IBM code or data is included. The evidence-bundle design follows arXiv 2604.19755.

## Intended uses

- Teaching and testing retrieval-augmented generation over policies, regulations and case files.
- Practising alert triage, narrative writing and citation checking.
- Training non-technical staff to use Microsoft 365 Copilot on ordinary Excel and Word files.

## Limits

- **Labels come from how the data was built.** No investigator reviewed them. A model that scores well here may not do well on real data.
- Amounts, timing and customer behaviour are simplified. Real laundering is rarer and messier.
- Names are invented. A name may match a real person by chance.
- The sanctions names are real entity names from a US government list, used only to practise screening. The list changes. This is not a current list.
- It covers Canada and the United States. Rules cited in the documents were read from the real text on the date in each file. They are not legal advice.

## Licence

Data: CC BY 4.0. Code: Apache-2.0. Regulation text, if you download it with the pack, keeps its own terms: US government work for 31 CFR Chapter X, and the Open Government Licence - Canada for the PCMLTFA and its regulations. FINTRAC guidance is downloaded on your machine and is not part of this dataset.

## Cite

Altman, E., Blanuša, J., von Niederhäusern, L., Egressy, B., Anghel, A., Atasu, K. (2023). Realistic Synthetic Financial Transactions for Anti-Money Laundering Models. arXiv:2306.16424. Please cite it if you use the patterns.

Dataset: BeCloudReady, labagent AML pack. TODO: release URL and version.
