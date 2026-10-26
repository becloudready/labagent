"""End-to-end local RAG: ingest, hybrid retrieval, answer. No cloud account needed.

    pip install 'labagent[rag-local]'
    labagent rag init --dir rag-lab && cd rag-lab
    python ../examples/01_rag_starter_lab.py

Set GROQ_API_KEY in .env for real answers; without any key you get a clearly marked mock answer.
This is the laptop version of the labagent Databricks packs (`labagent launch rag-sec-tutorial`).
"""
from pathlib import Path

from labagent.ai.rag import RAGLabClient
from labagent.ai.rag.config import load_dotenv

load_dotenv(Path(".env"))
client = RAGLabClient()

result = client.ingest_documents(["data"])
print(f"indexed {result.chunks} chunks from {result.documents} documents ({result.embedding_model}, {result.vector_store})")
for w in result.warnings:
    print("warning:", w)

for question in ("Why does hybrid search help with product codes?", "How big should chunks be?"):
    resp = client.query(question, top_k=3)
    print(f"\nQ: {question}\nA: {resp.answer}")
    for c in resp.chunks:
        print(f"   #{c.rank} {c.doc_id}  similarity={c.similarity}  keywords={c.keyword_score}")
    m = resp.metrics
    print(f"   {m.provider}/{m.model}  retrieval {m.retrieval_ms} ms  generation {m.generation_ms} ms  tokens {m.total_tokens}")

print("\nvector inspection:", client.inspect_vectors(result.doc_ids[0])["items"][0]["embedding_head"])
client.reset()
