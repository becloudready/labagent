"""Workspace content written by `labagent rag init` (and baked into the lab image). Kept as Python so nothing needs packaging.

One source of truth: the Dockerfile and the devcontainer both call `labagent rag init`, so the files a learner sees in the
web IDE are exactly these.
"""
import json
from typing import Dict, List, Union

ENV_TEMPLATE = """# labagent rag lab settings. Never commit real keys.
# Vector store: chroma (embedded, free) | qdrant | memory
VECTOR_DB_TYPE=chroma
EMBEDDING_MODEL_NAME=all-MiniLM-L6-v2

# Uncomment to keep the index between runs
# CHROMA_PATH=./.chroma

# LLM: leave every key empty to get a clearly marked mock answer, or set one of these.
# Free tier key: console.groq.com
GROQ_API_KEY=
# Groq model (default openai/gpt-oss-20b). If you get HTTP 404, your account lacks that model: list yours with GET /openai/v1/models
# GROQ_MODEL=
# Force a provider: groq | ollama | openai | anthropic | mock
# LLM_PROVIDER=ollama
# OLLAMA_MODEL=qwen2.5-coder:0.5b
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# Remote Qdrant (Qdrant Cloud): both values come from its dashboard
# QDRANT_URL=
# QDRANT_API_KEY=
"""

SECURITY_POLICY = """# Acme Corp Enterprise Security Policy

## Access control

Every employee gets a single identity managed by the company directory. Multi-factor authentication is required for all
systems. Shared accounts are not allowed. Access is granted by role and reviewed every quarter by the system owner.

## Passwords and secrets

Passwords must be at least 14 characters. Use the company password manager. Secrets such as API keys and database
credentials must never be stored in source code or chat tools. Store them in the secrets manager and rotate them every
90 days or immediately after a suspected leak.

## Devices

Company laptops use full disk encryption and install operating system updates within 14 days of release. Lost or stolen
devices must be reported to the security desk within one hour.
"""

DATA_HANDLING = """# Acme Corp Data Handling Standard

## Classification

Data is Public, Internal, Confidential or Restricted. Customer records, payment data and employee records are Restricted.
When unsure, treat the data as Confidential until the data owner decides.

## Storage and sharing

Restricted data is encrypted at rest and in transit. It may only be stored in approved systems and never on personal
devices or personal cloud storage. Share Confidential data only with named people, and use expiring links.

## Retention

Customer records are kept for seven years after the account closes, then deleted. Logs that contain personal data are kept
for 90 days. The data owner approves any exception in writing.

## Using AI tools

Do not paste Restricted data into public AI assistants. Approved internal AI tools may process Confidential data. Every
AI-generated answer used in a customer-facing document must be reviewed by a person.
"""

INCIDENT_LINES: List[str] = [
    "Acme Corp Incident Response Runbook",
    "",
    "1. Detect and report",
    "Anyone who suspects a security incident reports it to the security desk at once.",
    "The desk opens a ticket and assigns a severity from 1 (critical) to 4 (low).",
    "",
    "2. Contain",
    "For severity 1 and 2 the on-call engineer isolates the affected systems within 30 minutes.",
    "Do not delete evidence. Disable compromised accounts and rotate exposed secrets.",
    "",
    "3. Communicate",
    "The incident lead updates leadership every hour during a severity 1 incident.",
    "Legal decides whether customers or regulators must be notified. Notification of a",
    "personal data breach is made within 72 hours of discovery.",
    "",
    "4. Recover and review",
    "Restore service from clean backups. Hold a blameless review within five business days",
    "and track every follow-up action to completion.",
]


def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(lines: List[str]) -> bytes:
    """A minimal one-page text PDF (Helvetica 11pt). Enough for the lab's PDF-ingestion exercise, no PDF library needed."""
    content = "BT /F1 11 Tf 14 TL 56 780 Td\n" + "\n".join(f"({_esc(l)}) Tj T*" for l in lines) + "\nET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


MAIN_PY = '''"""Command-line RAG over data/sample_docs. Run from the lab folder:

    python src/main.py "What is our enterprise security policy?"
    python src/main.py --top-k 5 "How long do we keep customer records?"

Put your own .md, .txt or .pdf files in data/ and pass --data data to index them instead.
"""
import argparse
from pathlib import Path

from labagent.ai.rag import RAGLabClient
from labagent.ai.rag.config import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question")
    p.add_argument("--data", default=str(ROOT / "data" / "sample_docs"))
    p.add_argument("--top-k", type=int, default=3)
    args = p.parse_args()

    load_dotenv(ROOT / ".env")          # GROQ_API_KEY etc.; a missing key falls back to Ollama or a mock answer
    rag = RAGLabClient()                # vector_db="chroma", llm_provider picked from your keys
    result = rag.ingest(args.data)
    print(f"indexed {result.chunks} chunks from {result.documents} documents")
    for w in result.warnings:
        print("warning:", w)

    # TODO 1: print the answer.
    # TODO 2: print each retrieved chunk with its doc_id and similarity.
    # TODO 3: try --top-k 1 and --top-k 8. What changes in the answer?
    response = rag.query(args.question, top_k=args.top_k)
    print(response.answer)


if __name__ == "__main__":
    main()
'''


def sec_notebook() -> str:
    """Tutorial notebook: RAG over SEC 10-K filings (segments match the video outline)."""
    def md(src): return {"cell_type": "markdown", "metadata": {}, "source": src.splitlines(True)}
    def code(src): return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": src.splitlines(True)}
    cells = [
        md("# RAG over SEC 10-K filings\n\nYou will build a research assistant that answers questions from real annual reports (Form 10-K) of Apple, Pfizer, "
           "Walmart, JPMorgan and Chevron, with citations, filters and a quality check.\n\n"
           "**You need:** this web IDE and a free Groq key in `.env` (`GROQ_API_KEY=`). Without a key everything works except the written answers.\n\n"
           "**Time:** about 25 minutes.\n"),
        code("import os\nfrom pathlib import Path\nfrom labagent.ai.rag import RAGLabClient\nfrom labagent.ai.rag.config import load_dotenv\n\n"
             "load_dotenv(Path('../.env'))\nrag = RAGLabClient(vector_db='chroma', llm_provider='groq')   # Chroma in memory, MiniLM embeddings on CPU\n\n"
             "def show(chunks):\n    for c in chunks:\n        m = c.metadata\n        print(f\"#{c.rank}  {m.get('ticker')} {m.get('fiscal_year')}  Item {m.get('item')} ({m.get('section')})  \"\n              f\"similarity {c.similarity:.3f}  keywords {c.keyword_score:.2f}\")\n"),
        md("## 1. Why RAG for filings\n\nA 10-K runs to hundreds of pages. Pasting it into a prompt is slow, costly and often too long for the model. "
           "RAG keeps the documents outside the model, finds the few passages that matter for each question, and gives only those to the model.\n\n"
           "The loop: **load, chunk, embed, store, retrieve, generate, evaluate.** We do each step below.\n"),
        md("## 2. Load the data\n\nThe filings were already split into section-aware chunks and published as a versioned dataset. "
           "`fetch_snapshot` downloads it once and checks its SHA-256 every time, so everyone gets identical data.\n"),
        code("from collections import Counter\nfrom labagent.datasets import fetch_snapshot\n\nrows = fetch_snapshot('sec-10k-2026.10')\nprint(len(rows), 'chunks')\n"
             "print(Counter(r['meta']['ticker'] for r in rows))\nprint(Counter(r['meta']['section'] for r in rows))\nprint(sorted(Counter((r['meta']['ticker'], r['meta']['fiscal_year']) for r in rows)))\nprint()\nprint(rows[0]['title'])\nprint(rows[0]['meta'])\nprint(rows[0]['content'][:300], '...')\n"),
        md("Every chunk carries metadata: company, fiscal year, section (Item 1 Business, Item 1A Risk Factors, Item 7 MD&A) and the URL of the filing. "
           "Each company has **two fiscal years**, so the same question can match two near-identical passages. You will use the metadata for filters and citations.\n\n**Known gap:** the JPMorgan and Chevron MD&A sections are not in this dataset. We will see that show up in step 8.\n"),
        md("## 3. Embed and index\n\nAn embedding turns text into a list of numbers so that similar meanings are close together. "
           "`all-MiniLM-L6-v2` runs on the CPU here, so it costs nothing per call.\n"),
        code("import time\nt = time.time()\nresult = rag.ingest_chunks(rows, progress=lambda d, n: print(f'\\r{d}/{n}', end=''))\n"
             "print(f\"\\nindexed {result.chunks} chunks in {time.time() - t:.0f}s with {result.embedding_model} into {result.vector_store}\")\n"),
        md("## 4. Retrieve with vectors only\n\nThe question becomes a vector, and the store returns the closest chunks. `keyword_weight=0` means pure vector search.\n"),
        code("q = 'What patent expirations and generic competition risks does Pfizer disclose?'\nshow(rag.retrieve(q, top_k=5, keyword_weight=0))\n"),
        md("## 5. Break it: when vectors alone are not enough\n\nVectors capture meaning, not exact wording. Ask for a precise phrase and watch the ranking.\n"),
        code("q = '52- or 53-week fiscal year'\nprint('vectors only:')\nshow(rag.retrieve(q, top_k=3, keyword_weight=0))\n"
             "print('\\nvectors + keyword rerank:')\nshow(rag.retrieve(q, top_k=3, keyword_weight=0.3))\n"),
        md("The first run ranks a Walmart chunk first. The rerank adds a keyword-overlap score and puts the Apple chunks, which contain that exact phrase, on top. "
           "This is hybrid retrieval.\n\n**Your turn:** try `'iPhone 17 Pro'` and `'Permian Basin'`. Does the keyword score change the order? "
           "Sometimes it does not, and that is worth knowing.\n"),
        md("## 6. Filter with metadata\n\nSometimes you already know the company. A filter is exact, so no other company can appear.\n"),
        code("q = 'What are the main supply chain risks?'\nprint('no filter:')\nshow(rag.retrieve(q, top_k=5))\n"
             "print('\\nonly Walmart:')\nshow(rag.retrieve(q, top_k=5, where={'ticker': 'WMT'}))\n"),
        md("Each company has two years of filings. To compare only the latest year, filter on `fiscal_year` too.\n"),
        code("q = 'What patent expirations and generic competition risks does Pfizer disclose?'\nprint('both years:')\nshow(rag.retrieve(q, top_k=4, where={'ticker': 'PFE'}))\n"
             "print('\\n2025 only:')\nshow(rag.retrieve(q, top_k=4, where={'ticker': 'PFE', 'fiscal_year': '2025'}))\n"),
        md("**Your turn:** combine filters with a list, for example `where={'ticker': ['AAPL', 'WMT'], 'item': '1A'}`.\n"),
        md("## 7. Generate an answer with citations\n\nThe retrieved chunks go into the prompt. The model must answer only from them and say so when it cannot.\n"),
        code("resp = rag.query(\"What are Apple's main supply chain and manufacturing risks?\", top_k=5, where={'ticker': 'AAPL', 'fiscal_year': '2025'})\nprint(resp.answer)\n"
             "print('\\nprovider:', resp.metrics.provider, resp.metrics.model, '|', resp.metrics.generation_ms, 'ms |', resp.metrics.total_tokens, 'tokens')\n"
             "for c in resp.retrieved_chunks:\n    print('-', c.title, '->', c.url)\nfor w in resp.warnings:\n    print('warning:', w)\n"),
        code("# A question the filings cannot answer. A good system admits it.\nprint(rag.query('What is the recipe for Walmart\\'s store-brand cookies?', top_k=3).answer)\n"),
        md("## 8. Evaluate before you trust it\n\nWrite questions with the answer you expect, then measure how often retrieval finds a chunk from the right company and section. "
           "A score with a pass mark is how you know a change helped or hurt.\n"),
        code("cases = [\n"
             "    {'question': \"What are Apple's main supply chain and manufacturing risks?\", 'expect': {'ticker': 'AAPL', 'item': '1A'}},\n"
             "    {'question': 'How does Apple describe its products and services?', 'expect': {'ticker': 'AAPL', 'item': '1'}},\n"
             "    {'question': 'What credit and interest rate risks does JPMorgan Chase face?', 'expect': {'ticker': 'JPM', 'item': '1A'}},\n"
             "    {'question': \"How did JPMorgan Chase's net income and revenue change this year?\", 'expect': {'ticker': 'JPM', 'item': '7'}},\n"
             "    {'question': 'What patent expirations and generic competition risks does Pfizer disclose?', 'expect': {'ticker': 'PFE', 'item': '1A'}},\n"
             "    {'question': \"What are Pfizer's main product lines and business segments?\", 'expect': {'ticker': 'PFE', 'item': '1'}},\n"
             "    {'question': \"How do oil and gas price changes affect Chevron's earnings?\", 'expect': {'ticker': 'CVX', 'item': '7'}},\n"
             "    {'question': 'What climate and regulatory risks does Chevron identify?', 'expect': {'ticker': 'CVX', 'item': '1A'}},\n"
             "    {'question': \"How did Walmart's comparable sales and operating income perform?\", 'expect': {'ticker': 'WMT', 'item': '7'}},\n"
             "    {'question': 'What competition and supply risks does Walmart face?', 'expect': {'ticker': 'WMT', 'item': '1A'}},\n]\n\n"
             "ev = rag.evaluate(cases, top_k=5, min_hit_rate=0.6)\nprint(f'hit rate {ev.hit_rate}  ({ev.hits}/{ev.cases})  pass mark 0.6 -> {\"PASS\" if ev.passed else \"FAIL\"}')\n"
             "for m in ev.misses:\n    print('MISS:', m['question'])\n    print('      expected', m['expected'], '| got', m['got'])\n"),
        md("Two misses, and both are the data gap from step 2: there is no JPMorgan or Chevron MD&A in the dataset, so no retrieval setting can find it. "
           "Evaluation tells you whether to fix the **retrieval** or the **data**.\n\n**Your turn:** add three of your own questions to `cases`, then run `rag.evaluate(cases, keyword_weight=0)` "
           "and `rag.evaluate(cases, keyword_weight=0.3)` and compare.\n"),
        md("## 9. What it cost, and where next\n\nEmbeddings ran on this machine, so they were free. Answers cost fractions of a cent on Groq's free tier. "
           "The compute you are using is billed per second only while this IDE runs and stops when it goes idle.\n\n"
           "**Next:** the same pipeline runs managed on Databricks Lakebase (Postgres with pgvector) and Databricks Apps:\n\n"
           "```\npip install 'labagent[rag]'\nlabagent launch rag-sec-tutorial --name demo\n```\n"),
    ]
    for i, c in enumerate(cells):
        c["id"] = f"cell{i:02d}"          # nbformat 4.5 requires cell ids
    nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python"}},
          "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb, indent=1) + "\n"


LAB_README = """# RAG lab: build a retrieval-augmented generation system, one part at a time

You will build every part of a RAG system yourself, with numpy and a small embedding model, then compare it with a real vector
database. Nothing is hidden behind a library until you have written the same thing.

## Setup (once)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\\Scripts\\activate
python -m pip install --upgrade pip
pip install -e '.[rag-local]'                          # run from the labagent checkout
pip install jupyterlab
jupyter lab                                            # or open the notebooks in VS Code (choose the .venv kernel)
```

Optional, for real answers in notebook 4 onward: a free key from console.groq.com, pasted into `.env` as `GROQ_API_KEY=...`.
Without a key the lab still runs and shows a marked mock answer.

## The notebooks (do them in order, about 3 hours in all)

| # | Notebook | You build | Time |
|---|---|---|---|
| 1 | `01_chunking` | a chunker | 20 min |
| 2 | `02_embeddings` | cosine similarity and ranking | 20 min |
| 3 | `03_vector_store` | a vector store with numpy | 25 min |
| 4 | `04_generation` | the prompt and the full pipeline | 25 min |
| 5 | `05_chroma_and_filters` | a Chroma store and metadata filters | 20 min |
| 6 | `06_hybrid_search` | a keyword score and hybrid ranking | 25 min |
| 7 | `07_evaluation` | an evaluation and a settings comparison | 30 min |
| 8 | `08_break_it` | defences against old, missing and hostile documents | 30 min |
| 9 | `09_sec_filings_rag` | the packaged library on real SEC filings (optional) | 25 min |

## How the exercises work

Cells marked with a `TODO` are yours to write. Run the **check** cell under each one: it prints `ok` for each thing it verifies,
or stops with a hint. Each notebook starts from a working copy of the earlier exercises, so a stuck exercise never blocks
the next notebook. Write your reflections in the cells marked *Your answer*.

## Other things in this folder

- `data/sample_docs/`: the eight sample documents (the policies of an invented company). Put your own `.md`, `.txt` or `.pdf` files in `data/` to try them.
- `src/main.py`: a command-line RAG built on the labagent library, with three small TODOs.
- `labagent rag playground`: a visual page showing chunks, scores and answers side by side.
- `.env`: settings and keys. Never commit real keys.
"""


def workspace(solutions: bool = False) -> Dict[str, Union[str, bytes]]:
    """Relative path -> content for a fresh lab workspace. With solutions=True the instructor notebooks are added in solutions/."""
    from . import lab
    files: Dict[str, Union[str, bytes]] = {
        ".env": ENV_TEMPLATE,
        "README.md": LAB_README,
        "data/sample_docs/security_policy.md": SECURITY_POLICY,
        "data/sample_docs/data_handling.md": DATA_HANDLING,
        "data/sample_docs/incident_response.pdf": make_pdf(INCIDENT_LINES),
        **lab.sample_docs(),
        **lab.student(),
        "notebooks/09_sec_filings_rag.ipynb": sec_notebook(),
        "src/main.py": MAIN_PY,
    }
    if solutions:
        files.update(lab.solution())
    return files
