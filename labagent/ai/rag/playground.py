"""Streamlit playground: retrieval, top-K chunks and the LLM answer side by side.

Started by `labagent rag serve` (default port 8501). It uses the same RAGLabClient as the Python SDK and the notebook,
so what you see here is exactly what your code gets.
"""
import argparse
from pathlib import Path

import streamlit as st

from labagent.ai.rag import RAGLabClient
from labagent.ai.rag.config import load_dotenv


def _args() -> Path:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default=".")
    return Path(p.parse_known_args()[0].dir)


project = _args()
load_dotenv(project / ".env")
st.set_page_config(page_title="labagent RAG playground", layout="wide")


@st.cache_resource(show_spinner="Loading the embedding model and indexing data/ ...")
def build(path: str):
    client = RAGLabClient()
    result = client.ingest_documents([Path(path) / "data"])
    return client, result


client, ingested = build(str(project))
st.title("RAG playground")
st.caption(f"{ingested.documents} documents, {ingested.chunks} chunks | embeddings: {ingested.embedding_model} | store: {ingested.vector_store}")
for w in ingested.warnings:
    st.warning(w)
if ingested.chunks == 0:
    st.error(f"No documents found in {project / 'data'}. Run `labagent rag init` first or add .md/.txt files.")
    st.stop()

with st.sidebar:
    top_k = st.slider("Top K chunks", 1, 10, 3)
    doc = st.selectbox("Inspect a document's vectors", [""] + ingested.doc_ids)

question = st.text_input("Ask a question about your documents", "What is hybrid search and when should I use it?")
if st.button("Run", type="primary") and question:
    resp = client.query(question, top_k=top_k)
    left, right = st.columns(2)
    with left:
        st.subheader("Retrieved chunks")
        for c in resp.chunks:
            with st.expander(f"#{c.rank}  {c.doc_id}  similarity {c.similarity:.3f}  keywords {c.keyword_score:.2f}", expanded=c.rank == 1):
                st.write(c.text)
    with right:
        st.subheader("Answer")
        st.write(resp.answer)
        m = resp.metrics
        st.caption(f"{m.provider} {m.model} | retrieval {m.retrieval_ms} ms | generation {m.generation_ms} ms | "
                   f"tokens {m.total_tokens}{' (estimated)' if m.tokens_estimated else ''}")
        for w in resp.warnings:
            st.warning(w)
if doc:
    st.subheader(f"Vectors for {doc}")
    st.json(client.inspect_vectors(doc))
