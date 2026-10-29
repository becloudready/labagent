# AML lab: synthetic anti-money laundering data for RAG and Copilot

`labagent launch aml` builds a lab around a made-up credit union, **Maple Ridge Credit Union**. It generates customers, accounts, transfers and alerts, with an answer key for every alert. It also writes the policies, the case files and the regulation text. Every name and number is invented. The laws are real.

Two audiences use the same data:

| Mode | Who | What they get |
|---|---|---|
| `mode=rag` | Developers | An indexed corpus, a Python helper, three quality checks and a lab guide |
| `mode=copilot` | Staff at financial firms learning Microsoft 365 Copilot | A folder of Excel, Word and text files, a plain-English exercise guide, and a separate instructor folder with the answers |

```bash
pip install 'labagent[rag-local]'                     # rag mode: search and embeddings
pip install 'labagent[aml]'                           # copilot mode: writes Excel and Word files
labagent launch aml --name demo --param mode=rag --param jurisdiction=ca
labagent launch aml --name spring --param mode=copilot --param jurisdiction=ca --param size=small
labagent destroy aml --name demo
labagent "set up the AML copilot lab for Canada"      # plain English; it shows the command and asks first
```

Options: `jurisdiction=ca|us|both`, `size=small|medium`, `seed=42`, `generator=builtin|snapshot`, `regulations=auto|fetch|skip`, `solutions=no|yes` (rag mode: also write the solved notebooks). Same seed, same data.

## Synthetic AML dataset for RAG

In `mode=rag` the pack writes a lab folder:

| Path | What it holds |
|---|---|
| `data/` | Policy manual, alert procedure, report guide, regulation text and one case file per alert, as Markdown |
| `tables/` | `customers`, `accounts`, `transactions`, `alerts`, `kyc_profiles`, `sanctions_hits` as CSV |
| `answer_key/` | `ground_truth.csv`: the decision and the reason for every alert, kept apart |
| `aml_tools.py` | `AmlLab`: `ask`, `show`, `check`, `triage`, `score`. The finished assistant: the demo |
| `notebooks/` | Four build-it-yourself notebooks: the lab |
| `solutions/` | The solved notebooks, only with `--param solutions=yes` (for instructors) |

The corpus is indexed with the same client as the local RAG lab (`labagent.ai.rag`). Policies and regulations are chunked by section. Case files keep only the summary and the customer profile as text. Transactions stay in tables, because a table is the right shape for them.

Learners do three tasks: ask policy questions and read the citations, pull the **evidence bundle** for an alert, and draft a report narrative that cites real transaction IDs and sections. The bundle holds the customer profile, the alert trigger, the transactions around it, the matching policy and rule text, and similar earlier cases. The design follows the explainable triage paper (arXiv 2604.19755).

### Two ways to use the folder: the demo and the lab

- **The demo is `aml_tools.AmlLab`.** It is a finished assistant, so learners see what a good result looks like before they build one. The tasks in `LAB_GUIDE.md` use it.
- **The lab is `notebooks/`.** Learners write the same parts themselves, and each part is tested against the data and the answer key:

| # | Notebook | The learner writes | Tested against |
|---|---|---|---|
| 1 | `01_policy_search` | a search with metadata filters (country, document type), and a hit-rate measure | the launch's own 14-question set |
| 2 | `02_evidence_bundle` | the transactions around an alert (own account, then following the money), similar earlier cases, the whole bundle | recall of the answer key's key transactions (0.72 own-account only, 1.00 with hops), and a canary test for answer leakage |
| 3 | `03_citations_and_narrative` | a citation checker, a gate that cannot be passed by saying nothing, the narrative prompt | invented IDs and sections, and a real ID from the wrong alert |
| 4 | `04_triage_and_evaluation` | a scorer, a rule that decides, a strict parser for a model's decision, an error analysis by pattern | the answer key and a majority-label baseline |

They use the same stub, check and solution format as the [RAG lab](rag-local.md), and start from working copies of the earlier exercises. Learners
need the RAG lab's `pip install -e '.[rag-local]'` and `jupyterlab`. Instructor notes: [aml-lab-instructor.md](aml-lab-instructor.md).
`destroy` removes the notebooks unless the learner has edited them: an edited notebook is their work and stays.

### Quality checks

Every launch runs three checks and reports them in the result:

1. **Retrieval hit rate** on 14 policy and regulation questions per country, each with a known answer. Gate: 0.6. With a real embedding model (all-MiniLM-L6-v2, top 5, seed 42) the hit rate was 0.79 to 0.86 for Canada, the United States and both. With the keyword-only fallback embedder it is about 0.43, so the gate is reported but not applied there.
2. **Citation validity.** Every transaction ID, regulation section and policy section a reference narrative cites must exist. Gate: 1.0.
3. **Triage agreement** with the answer key on 12 alerts (4 per label). Reported only. A fixed reference rule agrees on 99.9 percent of 1,152 alerts across 16 generated datasets. That shows the evidence is enough to decide. It does not measure a model. Set `llm=env` to score your chat model the same way.

## AML transaction monitoring data with labels

The built-in generator is pure Python, needs no Java and no network, and is deterministic for a seed.

| Size | Customers | Accounts | Transfers | Alerts |
|---|---|---|---|---|
| `small` | 300 | 429 | about 42,000 | 72 |
| `medium` | 2,000 | 2,889 | about 281,000 | 552 |

Normal activity comes first: payroll, rent, card spending, business sales and supplier payments. Labelled cases are laid on top:

- **The 8 laundering patterns** from Altman et al. (2023): fan-out, fan-in, gather-scatter, scatter-gather, cycle, random, bipartite and stack.
- **Two disguised-payment cases**: illegal money paid out as payroll, or as supplier invoices just under the 10,000 limit.
- **Name matches** to a fixed set of 12 entity names from the OFAC SDN list. Fuzzy variants are included. Every row is tagged TEST DATA.
- **Monitor look-alikes**: many small gifts, cash at a cash business, a loan and its repayment, a payment passed to a supplier.
- **Dismiss cases**: a house sale, a bonus, a repeat family payment, a tuition wire.

Each alert has a label (`escalate`, `monitor` or `dismiss`), a pattern, the accounts and transactions involved, and a plain-English reason. In the small set: 22 escalate, 20 monitor and 30 dismiss.

**The labels come from how the data was built, not from an investigator.** They show what a good answer looks like. They do not show that a model which scores well here will do well on a real bank's data.

Tables are written as CSV, and as Parquet when `pyarrow` is installed. The schema is the same for every generator.

## Copilot training data for banking

In `mode=copilot` the pack writes two folders and two zips:

```
spring/                     learner folder (spring.zip)
  Policies/                 policy manual, alert procedure, report guide (.docx)
  Regulations/              excerpts of the rules, and FINTRAC guidance if fetched (.docx)
  Cases/                    one Excel workbook and one Word file per case, plus a case list
  Data/                     all transfers, customers, accounts, alerts; one file per month
  Exercise Guide.docx       13 exercises, no answers
spring-instructor/          instructor folder (spring-instructor.zip)
  Exercise Guide with Answers.docx
  Instructor Answer Key.docx
```

Every workbook holds real Excel tables, because Copilot in Excel works on tables. Learners upload the folder to OneDrive or SharePoint and turn on AutoSave. Microsoft documents these limits: Copilot in Excel needs `.xlsx` files in OneDrive or SharePoint with AutoSave on, files under 150 MB, and at most five files in one multi-file action. The guide tells learners about each one.

The 13 exercises cover four parts: policies, regulations, cases and data. Examples: "Summarize this case and list the red flags", "Which section covers cash deposits just under 10,000?", "Find all transfers over 5,000 to new payees in March". Each part ends with a **catch the AI's mistake** exercise: a short AI-style draft with planted errors that learners find by checking the files. The expected answers are computed from the data.

The learner files use no technical terms (no RAG, vectors, Databricks or code). The launch fails if a learner file contains one, if an instructor file is in the learner folder, or if a workbook has no table.

I have not run these files against a live Microsoft 365 Copilot tenant. Check the large Transactions file in your tenant before a class, and use the monthly files if Copilot struggles.

## FINTRAC STR narrative practice

For Canada, the guide to writing the narrative asks six questions: who, what, when, where, why it is suspicious, and how the money moved. The rules the pack cites were read from the real text:

- PCMLTFA s. 7 requires a report when there are reasonable grounds to suspect.
- PCMLTF STR Regulations s. 9: send the report as soon as practicable after the measures that let you establish those grounds.
- PCMLTF Regulations s. 10 and s. 132: a financial entity keeps a record of $10,000 or more in cash, and reports it within 15 days.
- PCMLTF Regulations s. 148: keep records at least five years.

The US version cites 31 CFR 1020.320 (a SAR within 30 calendar days of detecting the facts), 31 CFR 1010.311 (currency transactions of **more than** $10,000) and 31 CFR 1010.306 (15 days). Note the difference: Canada says "$10,000 or more", the United States says "more than $10,000". The data contains cases where that difference matters.

In `mode=rag`, `lab.check(text)` tests a narrative: every transaction ID and section it cites must exist. Section numbers are checked against `labagent/packs/aml/data/reg_index.json`.

## Sources and licences

| Source | How it is used | Licence |
|---|---|---|
| Built-in generator, policies, case files | Written by labagent | CC BY 4.0 for published data; Apache-2.0 for code |
| OFAC SDN list | 12 entity names, kept in the repository with their list uids and the list date | US government work (public domain) |
| eCFR, 31 CFR Chapter X parts 1010 and 1020 | Downloaded at launch from govinfo. Redistributable, so it may go in a snapshot | US government work. The eCFR is an unofficial compilation |
| PCMLTFA and two regulations (Justice Laws XML) | Downloaded at launch. Redistributable with attribution | Open Government Licence - Canada. Not the official version |
| FINTRAC indicators and STR guidance | Downloaded on the instructor's machine at launch, never committed | FINTRAC's terms (not reviewed) |
| IBM AMLSim and Altman et al. (arXiv 2306.16424) | Source of the 8 patterns and the idea of disguised payments. No IBM code or data is included | AMLSim is Apache-2.0 |
| Triage paper (arXiv 2604.19755) | Design of the evidence bundle and the three labels. No code or data used | CC BY 4.0 (paper) |

A snapshot can only contain sources marked "bundled" in `labagent/packs/aml/licences.py`. Building a snapshot with a fetch-only source fails. OpenSanctions (CC BY-NC 4.0) is not used. FFIEC manual sections, the IBM Kaggle dataset, SAML-D and Elliptic2 are not used either: their redistribution terms were not verified.

## Not built yet

- Databricks Lakebase platform and the `rag-ide` option.
- `generator=amlsim` (run IBM AMLSim in Docker) and the optional importers.
- A published `aml-synthetic` snapshot. Until it exists, `generator=snapshot` stops with a clear message.
- Upload to SharePoint through Microsoft Graph.
