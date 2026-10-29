# AML RAG lab: instructor notes

For the instructor of the build-it-yourself notebooks in an AML lab folder (`labagent launch aml --param mode=rag`). Add
`--param solutions=yes` to get `solutions/`. The guide for learners is `LAB_GUIDE.md` in the lab folder; the data and the finished
tool are described in [aml.md](aml.md).

**Audience:** developers who have done the [RAG lab](rag-local.md) (at least notebooks 1 to 6) and want to apply it to a regulated
domain. **Not for:** AML investigators who need to learn to *use* an AI assistant (use `mode=copilot`).
**Prerequisites:** Python functions, the RAG lab's `TinyStore` idea, `pip install -e '.[rag-local]'` and `jupyterlab`. No AML knowledge:
the policy text in the lab explains each rule, and the first notebook has learners read it.

## The design: demo first, then build

The launch gives learners a **finished** assistant (`aml_tools.AmlLab`). They use it for 10 to 15 minutes (the tasks in `LAB_GUIDE.md`
and the top of notebook 1), so they know what good looks like. Then they build the parts themselves and compare with it at the end of
each notebook. Do not skip the demo: without it the build has no target.

## Learning objectives

By the end a learner can:
1. filter retrieval by metadata and explain why that is a correctness rule in compliance work;
2. build an evidence bundle, **measure its completeness** against known-bad cases, and test it for answer leakage;
3. verify the references in generated text (existence and support) and design a gate that silence cannot pass;
4. score decisions against a key, beat a baseline, parse model output strictly, and read errors by pattern;
5. say what a synthetic benchmark does and does not show.

## Time arithmetic

Estimates from a timed solution run and a build, **not from a classroom**. The solutions run in about 15 seconds per notebook.

| Item | Minutes |
|---|---|
| Demo and the guide's tasks | 15 |
| Notebooks 1 to 4 (25 + 35 + 30 + 35) | 125 |
| Setup, if not done beforehand | 20 |
| Break | 15 |
| Live review of the "Your turn" questions | 30 |
| Float | 15 |
| **Total wall-clock** | **220 (3 h 40 min)** |

Run it as one half-day, or two sessions (notebooks 1 and 2, then 3 and 4). Notebook 2 is the longest and has the most to discuss.

## Before the day

1. `labagent launch aml --name demo --param mode=rag --param jurisdiction=ca --param solutions=yes` and run `solutions/` yourself.
   `regulations=auto` downloads the regulation text from two government sites; if the room has no internet, launch beforehand and copy the folder.
2. A Groq key for you (and optionally a shared fallback). Without a key notebooks 3 and 4 show the mock provider, and the checks still pass.
   That is deliberate: the build is the lesson, not the model.
3. Choose `jurisdiction`: `ca` or `us` for a single-rule room. `both` is a good stretch (the jurisdiction filter then matters), but it makes
   the rule in notebook 4 reach about 0.81, not 0.89.

## Numbers you will see (seed 42, size small, Canada)

| Measure | Value |
|---|---|
| Policy hit rate, 14 questions, MiniLM, top 5 | 0.86 (two misses: record keeping, the six narrative questions) |
| Key-transaction recall, own account only | 0.72 (worst alert 0.20) |
| Key-transaction recall, 5 hops, common parties cut | 1.00, about 21 rows per bundle |
| Always guessing "dismiss" | 0.42 |
| The solution's simple rule | 0.89 (US 0.89, both 0.81) |
| The packaged reference rule on the learner's bundles | 1.00 |

Numbers move a little with the embedding model version. The checks use thresholds, not exact values.

## Where learners get stuck

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` in the launch | Old checkout. The rag-mode import was broken until the fix in this branch | Pull `main` |
| `FileNotFoundError: config.json` in a notebook | Jupyter was started outside the lab folder | Start `jupyter lab` in the lab folder, or open the notebook from `notebooks/` |
| `No module named aml_tools` | Same cause | Same fix |
| Notebook 2 recall stays at 0.72 | `hops` never reaches the code that follows other accounts | The loop must start again from the newly reached accounts, not from the alert account |
| Notebook 2 recall is high but the bundle has 500 rows | The common-party rule is missing | Skip transfers where either end is common, and do not step into common parties |
| Notebook 3 gate passes on mock output | It does not: `acceptable` rejects text with no citations | Check that `acceptable` requires at least two transactions and a rule |
| Notebook 4 stuck below 0.75 | Rules are too coarse | Print the disagreements, read two `reason` fields, add one rule at a time |

## Judgment questions for live review

**Notebook 2: the bundle leaks if it uses the answer key for the alert itself. Is using the key for earlier cases also leakage?**
Target: no, if the earlier decisions really were made before this alert, as in practice. Yes, if "earlier" is judged by id rather than date, or if the data was split
carelessly. Fork: "then use all other alerts as examples." That leaks the future; ask what the model would see in production.

**Notebook 2: why is recall of the key transactions the right test for a bundle?**
Target: because the bundle sets the ceiling for every later step. Fork: "just give the model everything." Ask what 42,000 transfers cost in tokens and what the model does with them.

**Notebook 3: a citation check passes a text that cites nothing. What else catches a wrong claim?**
Target: a separate check that every amount is in the evidence or recomputable, and a human reviewer. Fork: "a second model checks the first." Useful, correlated errors, and
costs the same again; use as a filter, not a guarantee.

**Notebook 4: the reference rule scores 1.0. Is AML triage solved?**
Target: no. The labels were generated from the same patterns the rule reads. Fork: "then it will work on real data." Ask for three differences between this data and a real credit union
and what each does to the number.

## What the lab does not cover

Entity resolution, network analytics at scale, model monitoring, regulatory expectations on model risk, and how to run a real case review. Say so.

## Honest limits of this build

- Timings are estimates, not measured with learners.
- The model-based steps were exercised with the mock provider only. The parser and the prompt are checked structurally, not against a real model's output.
- The "family support" dismiss cases are the main remaining error of the solution's rule. That is intended: it gives the error analysis something to find.
- Tested on macOS with Python 3.9. Windows and Python 3.11 or newer are untested.
