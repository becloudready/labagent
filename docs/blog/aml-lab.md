# A synthetic AML lab you can run in one command

Draft for becloudready.com and dev.to. Not published. Add the release link before you post.

Real anti-money laundering data is private, and rightly so. That makes it hard to teach with. You cannot hand a class of bank staff, or a room of developers, a bank's transaction history.

So we built a made-up one. `labagent launch aml` creates a fictional credit union, **Maple Ridge Credit Union**, with customers, accounts, transfers and alerts. Every name and number is invented. The laws are real.

## What the data contains

The small set has 300 customers, about 42,000 transfers and 72 alerts. Each alert has an answer: escalate, monitor or dismiss, with a plain-English reason.

The suspicious cases use the 8 patterns from the IBM paper on realistic synthetic AML data: fan-out, fan-in, gather-scatter, scatter-gather, cycle, random, bipartite and stack. We add laundering that looks like payroll or supplier invoices. We also add cases that look suspicious but are not: a few small gifts, a loan and its repayment, a house sale.

One honest limit: the answers come from how we built the data. No investigator checked them. They show what a good answer looks like. They do not prove a model will work on a real bank.

## One worked example

Alert `AL00006` fired on 6 March for the rule "many senders to one account". The account belongs to a teacher whose profile expects about 5,400 USD of deposits a month. In three days, five transfers arrived:

| ID | Date | From | Amount | Note on the transfer |
|---|---|---|---|---|
| T0007398 | 3 March | ACC000139 | 3,775.35 | Family support |
| T0007414 | 3 March | X90007 | 3,339.08 | E-transfer sent |
| T0007448 | 4 March | X90008 | 3,474.36 | Family support |
| T0007485 | 4 March | ACC000086 | 3,535.15 | Loan repayment |
| T0007552 | 5 March | ACC000150 | 3,570.73 | Loan repayment |

The total is 17,694.67, which is 3.3 times the monthly profile. The senders are five different accounts, three of them customers of the credit union who do not usually pay each other. The notes say family support and loan repayment, but this account never exchanged money with any of the five senders before this week, so no loan shows in the data. The answer key says **escalate**.

A good narrative says who, what, when, where, why it is suspicious and how the money moved, and it cites the five transaction IDs. The lab checks that every ID you cite exists.

## Two ways to use it

**For developers.** `mode=rag` builds a corpus of policies, regulation text and case files, indexes it, and runs three checks: does search find the right section, are the citations real, and does a fixed rule reach the answer key's decision from the evidence. The learner then asks questions, pulls the evidence for an alert, and drafts a narrative. With a real embedding model, search found the right section for 79 to 86 percent of our test questions. The misses are useful teaching: "how long must records be kept" lands on the wrong section.

**For staff.** `mode=copilot` writes a folder of ordinary Excel and Word files, with one workbook per case and a guide of 13 plain-English exercises. Each part ends with an AI draft that has planted mistakes, and the learner finds them by checking the files. One mistake is a single small word: Canada's cash report rule says "$10,000 or more", and the US rule says "more than $10,000". Copilot, like people, mixes them up. We have not run these files against a live Copilot tenant yet, so test the big file before a class.

## Try it

```bash
pip install 'labagent[rag-local,aml]'
labagent launch aml --name demo --param mode=rag --param jurisdiction=ca
```

The lab guide is in the repository: docs/aml.md. Credit to IBM's AMLSim and the authors of arXiv 2306.16424 for the patterns.
