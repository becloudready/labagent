# RAG lab: instructor notes

For the instructor of the [local RAG lab](rag-local.md). Learners get `notebooks/`; you get `solutions/` with
`labagent rag init --dir rag-lab --solutions`.

**Audience:** engineers who can write Python functions (loops, lists, dicts, a little numpy). **Prerequisites:** a laptop with
Python 3.9+, `git`, 3 GB free disk, internet for the install and a 90 MB model download. No ML knowledge. No cloud account.

## Learning objectives

By the end a learner can:
1. cut documents into chunks and explain what size and overlap trade;
2. explain an embedding as a vector, compute cosine similarity, and say where embeddings fail (numbers, negation);
3. describe what a vector database does, because they wrote one, and say when brute force stops working;
4. build a grounded prompt, and test that the model refuses when the sources do not answer;
5. use metadata filters, and explain why exact words need a keyword score alongside vectors;
6. measure retrieval with hit rate and MRR, and say how much a 20-case result can be trusted;
7. defend against old versions, uncovered questions and prompt injection, and name what those defences do not guarantee.

## Business value to say out loud

A RAG system that answers wrongly with confidence is worse than none. The lab ends on measurement and failure modes because those
are what separate a demo from something a team can run: tested retrieval, a refusal rule, and document versioning.

## Time arithmetic (do this before you schedule)

These are **estimates from a build and a timed solution run, not from a classroom**. The solutions run in about 10 seconds per
notebook; a learner is slower. Plan on the estimates and add the overhead.

| Item | Minutes |
|---|---|
| Notebooks 1 to 8 (20 + 20 + 25 + 25 + 20 + 25 + 30 + 30) | 195 |
| Setup, install and the model download, with mixed laptops | 25 |
| Intro (what RAG is, the map of the day) | 15 |
| Two breaks | 30 |
| Live review of the "Your turn" questions (about 8 minutes each across the day) | 40 |
| Float | 15 |
| **Total wall-clock** | **320 (5 h 20 min)** |

That is a full day with lunch, or two half days. Notebook 9 (SEC filings) is an extra 25 minutes and needs a Groq key and a
dataset download: use it as homework. If you must cut, cut notebook 5 (Chroma) first: nothing later needs it, and say what Chroma
adds in two minutes.

**This is a lab, not a demo.** Learners write code; the install is the part most likely to eat the time. Do the setup the evening
before, or provide the lab in a pre-built environment. The `rag-ide` pack gives each learner a browser VS Code with the lab already
installed.

## Before the day

1. Run `labagent rag init --dir rag-lab --solutions` and run notebooks 1 to 8 in `solutions/` yourself. They must finish with every
   check `ok`.
2. Get a Groq key for yourself, and one shared fallback key if the room has none. A free key is rate-limited; 15 people asking
   at once in notebook 4 will hit it. The mock answer is a working fallback: it shows the prompt, which is the point.
3. Test a corporate laptop on the network you will use: `huggingface.co` is the usual blocker.

## Where learners get stuck (and the fix)

| Symptom | Cause | Fix |
|---|---|---|
| `pip install -e` fails with "setup.py not found" | The pip bundled with macOS Python 3.9 is too old | `python -m pip install --upgrade pip` first |
| `ModuleNotFoundError: sentence_transformers` or `streamlit` | Installed without the extra | `pip install -e '.[rag-local]'` |
| The notebook uses a different Python | Jupyter's kernel is not the `.venv` | Pick the `.venv` kernel (VS Code) or run `jupyter lab` from the activated venv |
| `HTTP 404` from Groq | The key's account lacks the default model | Set `GROQ_MODEL` in `.env` to one the account has |
| `Check failed` | The check is working | Read the hint. It names what to look at |
| Different numbers from the notes | Different model version or chunk size | The shape of the result matters, not the exact decimals |

## Judgment questions for live review

Each has a target answer and a fork where reasonable people disagree. Run the fork; do not just announce the target.

**Notebook 1, exercise 3: chunk size for three document types.**
Target: contract, small chunks plus a way to bring in defined terms; FAQ, one entry per chunk; chat log, window by turns not by
characters. Fork: "just use bigger chunks and a bigger model." True up to cost and dilution. Ask what the prompt costs at k=5.

**Notebook 3: which top hit does not answer the question?**
Target: the stolen-laptop and "work from Spain" hits are partial; the lunch-menu hit scores about 0.24 yet is shown as a result.
Fork: "the score is a confidence." It is not; it is a closeness. Come back to this in notebook 8.

**Notebook 4: cost levers.**
Target: lowering `k` risks missing the answer; shrinking chunks risks splitting it. Neither is free. Measure in notebook 7.

**Notebook 6: which `alpha`.**
Target: a small weight (about 0.1 in the table; 0.3 already broke one plain-language query) here, because exact-token questions need a nudge and plain-language ones suffer when keywords
dominate. Fork: the five-query table is too small to choose with; notebook 7 is the answer. Ask what changes with a corpus full of
product codes.

**Notebook 7: which setting would you ship, and how sure?**
Target: read the table at top-1. With the keyword weight, chunk sizes from 300 up to 3000 characters all reached 0.95 on these 20
cases, while 150 reached 0.85. Without it, 300 and 500 (0.90) beat 150 (0.75) and 1000 (0.80). So the weight mattered more than the size,
and 150 was the one clear loser. But one case is worth five points, so differences of one case are noise. Fork: "the table shows 0.95
so it is 95% accurate." It is not: it is a hit rate on questions written by the people who built the lab, on a short list.

**Notebook 8: which mistake is worse, answering wrongly or refusing a good question?**
Target: it depends on the cost of each. Medical or legal: refuse more. Internal FAQ: answer more and show sources. Fork: "set the
threshold once." The threshold belongs to the embedding model, the chunk size and the scoring; change any and re-measure.

**Notebook 8: is the injection defence enough?**
Target: no. It lowers the chance; the real protection is limiting what the model can do and who can add documents. Fork: "use a
better model." Better models resist more, none perfectly. Ask what an attacker needs: write access to one indexed file.

A live-reviewed quiz costs about 8 to 10 minutes of discussion each. The 40 minutes in the table covers about five.

## What the lab does not cover

Re-ranking with a cross-encoder, query rewriting, multi-turn memory, tables and scanned PDFs, answer-quality evaluation, and
production concerns (access control per document, freshness pipelines, observability). Say so; point to notebook 7's last section.

## Honest limits of this build

- The timings above were not measured with learners.
- The Groq path (real answers) was not exercised in the build; the mock provider was. The prompts are checked structurally.
- Tested on macOS with Python 3.9. Windows and Python 3.11+ are untested.
- Scores shift with the embedding model version; the checks use ranges and orderings, not exact numbers.
