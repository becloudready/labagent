# Local RAG lab: build a RAG system, one part at a time (zero cost)

A hands-on lab that runs on your laptop. No cloud account, no database bill, no per-token embedding fees. You build every part of
a RAG system yourself (chunker, similarity search, vector store, prompt, hybrid search, evaluation, defences), then compare it
with a real vector database and with the packaged library.

**Audience:** engineers who can write Python functions and want to understand RAG by building it. No machine-learning background
needed. **Instructor notes:** [rag-lab-instructor.md](rag-lab-instructor.md).

## Set up

```bash
git clone https://github.com/becloudready/labagent && cd labagent
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip                    # the pip that ships with macOS Python 3.9 cannot install this package in editable mode
pip install -e '.[rag-local]'                          # Chroma, sentence-transformers (PyTorch), Streamlit, Pydantic, pypdf
pip install jupyterlab                                 # or open the notebooks in VS Code and choose the .venv kernel
labagent rag init --dir rag-lab                        # creates the lab folder
cd rag-lab && jupyter lab
```

Needs Python 3.9 or newer (3.11 or 3.12 recommended), `git`, about 3 GB of disk, and internet on the first run (the install, and
a 90 MB embedding model from Hugging Face). The local lab is not in the PyPI release 0.2.1, so install from a checkout.

Optional: a free key from console.groq.com in `rag-lab/.env` as `GROQ_API_KEY=...` for real answers from notebook 4 onward. With no
key every notebook still runs and shows a clearly marked mock answer.

## The notebooks

| # | Notebook | The learner writes | Concept |
|---|---|---|---|
| 1 | `01_chunking` | `chunk_fixed`, `chunk_paragraphs` | size, overlap and structure trade precision for context |
| 2 | `02_embeddings` | `cosine`, `rank` | meaning as vectors; where embeddings are blind |
| 3 | `03_vector_store` | `TinyStore.add`, `TinyStore.search` | what a vector database does; brute force and its limit |
| 4 | `04_generation` | `build_prompt`, `answer` | grounding, "I don't know", citations, prompt cost |
| 5 | `05_chroma_and_filters` | `ChromaStore` | what a real database adds; metadata filters |
| 6 | `06_hybrid_search` | `keyword_score`, `hybrid_search` | exact words versus meaning; the weight; the pool limit |
| 7 | `07_evaluation` | `evaluate`, own test cases | hit rate and MRR; comparing settings honestly |
| 8 | `08_break_it` | `latest_only`, `should_answer`, `looks_like_injection`, `build_safe_prompt` | stale, missing and hostile documents |
| 9 | `09_sec_filings_rag` | nothing (optional) | the packaged library on real SEC filings |

Each exercise is a function with a `TODO`, followed by a **check** cell that prints `ok` for each thing it verifies or stops with a
hint. Each notebook starts from a working copy of the earlier exercises, so a stuck exercise never blocks the next notebook.

`init` creates:

| Path | What it is |
|---|---|
| `README.md` | The learner's guide: setup, the notebook map, how the checks work |
| `.env` | Settings and API keys (mode 600) |
| `data/sample_docs/` | Eight documents from an invented company: seven Markdown files and one PDF |
| `notebooks/01_*.ipynb` to `09_*.ipynb` | The lab |
| `src/main.py` | A command-line RAG on the labagent library, with three TODOs |

It refuses to overwrite existing files without `--force`. Instructors add `--solutions` to also write the solved notebooks to
`solutions/`. Notebook 8 writes two deliberately bad documents to `data/untrusted/`.

Other commands: `labagent rag playground` (alias `serve`) opens a visual page on http://localhost:8501 with chunks, scores and
answers side by side. `labagent rag ide --dir rag-lab` opens a browser VS Code on the lab (Docker, or `code-server` on your PATH).

## The packaged library

The notebooks build this by hand. For real work the same pipeline is in the library:

```python
from labagent.ai.rag import RAGLabClient

client = RAGLabClient()                       # settings come from the environment
client.ingest("data/sample_docs")            # .md, .txt, .rst and .pdf files or folders. ingest_documents([...]) takes a list; aingest_documents() is the async form
resp = client.query("What is hybrid search?", top_k=3)
resp.answer, resp.chunks, resp.metrics        # chunks carry similarity, distance, keyword_score
client.inspect_vectors("security-policy")     # chunk ids, text, first 8 embedding values
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
