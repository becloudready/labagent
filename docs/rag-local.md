# Local RAG lab (zero cost)

A RAG lab that runs on your laptop. No cloud account, no database bill, no per-token embedding fees.

Install from a checkout (the local lab is not in the PyPI release 0.2.1):

```bash
git clone https://github.com/becloudready/labagent && cd labagent
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e '.[rag-local]'         # Chroma, sentence-transformers, Streamlit, Pydantic, pypdf
labagent rag init --dir rag-lab       # creates the lab folder (see below)
cd rag-lab && labagent rag serve      # playground on http://localhost:8501
```

`init` creates:

| Path | What it is |
|---|---|
| `.env` | Settings and API keys (mode 600). Leave every key empty to get a marked mock answer |
| `data/sample_docs/` | Three sample documents: `data_handling.md`, `security_policy.md`, `incident_response.pdf` |
| `notebooks/01_build_your_first_rag.ipynb` | Build a RAG step by step |
| `notebooks/02_sec_filings_rag.ipynb` | A larger exercise on SEC filings |
| `src/main.py` | A command-line RAG with three TODOs: `python src/main.py "What is our enterprise security policy?"` |

It refuses to overwrite existing files unless you pass `--force`.

The first run downloads the embedding model (`all-MiniLM-L6-v2`) from Hugging Face, so the first ingest is slow.

Other ways in: `labagent rag ide --dir rag-lab` opens a browser VS Code on the lab (Docker, or `code-server` on your PATH). `labagent rag playground` is the same as `serve`; `--port` changes 8501.

For the Databricks version (Lakebase, pgvector, a deployed app) use `labagent launch rag-sec-filings`. See [packs.md](packs.md).

## Python API

```python
from labagent.ai.rag import RAGLabClient

client = RAGLabClient()                       # settings come from the environment
client.ingest("data/sample_docs")            # .md, .txt, .rst and .pdf files or folders. ingest_documents([...]) takes a list; aingest_documents() is the async form
resp = client.query("What is hybrid search?", top_k=3)
resp.answer, resp.chunks, resp.metrics        # chunks carry similarity, distance, keyword_score
client.inspect_vectors("hybrid-search")       # chunk ids, text, first 8 embedding values
client.reset()
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `VECTOR_DB_TYPE` | `chroma` | `chroma`, `qdrant` or `memory` |
| `EMBEDDING_MODEL_NAME` | `all-MiniLM-L6-v2` | Local CPU model. `hashing` is a dependency-free test embedder |
| `CHROMA_PATH` | unset | Set it to persist the index. Unset means in memory |
| `QDRANT_URL`, `QDRANT_API_KEY` | unset | Remote Qdrant. Not tested against a live server |
| `RAG_COLLECTION` | `labagent_rag` | Collection name in the vector store |
| `LLM_PROVIDER` | auto | `groq`, `ollama`, `openai`, `anthropic` or `mock` |
| `GROQ_API_KEY`, `GROQ_MODEL` | unset, `openai/gpt-oss-20b` | Groq free tier. A 404 means your account lacks the model: set `GROQ_MODEL` to one it has |
| `OLLAMA_URL`, `OLLAMA_MODEL` | `http://localhost:11434/v1`, `qwen2.5-coder:0.5b` | Local Ollama |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | unset, `gpt-4o-mini` | OpenAI |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | unset, `claude-haiku-4-5-20251001` | Anthropic |

With no `LLM_PROVIDER`, the order is Groq, OpenAI, Anthropic (whichever has a key), then local Ollama, then a clearly marked mock answer. A provider that errors falls through to the next, and the reason is returned in `resp.warnings`.

## How retrieval works

1. Chunk: paragraph-aware, about 800 characters, 100 characters of overlap.
2. Embed and store: batches of 64 into the vector store.
3. Query: fetch 4x `top_k` nearest chunks by cosine similarity, then rerank by `0.7 * similarity + 0.3 * keyword overlap`. This is a rerank of vector candidates, not a full keyword index. An exact term that the vector search does not surface will not be found.

## Add a vector store

Subclass `BaseVectorAdapter` (`upsert`, `query`, `get_doc`, `reset`, `count`) in `labagent/ai/rag/adapters.py` and add it to `make_adapter()`.

## Limits

- Without `sentence-transformers` the client falls back to a hashing embedder that matches shared words, not meaning, and says so in `warnings`.
- Token counts are estimated (length / 4) when a provider returns no usage. `metrics.tokens_estimated` tells you which.
- PDFs are read when `pypdf` is installed (the `rag-local` extra includes it). Without it, and for Office files and other types, the file is skipped and listed in `result.skipped`.
- The `.env` file never overrides a variable that is already set in your shell.
