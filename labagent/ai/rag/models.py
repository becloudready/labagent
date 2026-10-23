"""Structured results returned by RAGLabClient."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    doc_id: str
    chunk_id: str
    text: str
    similarity: float = Field(description="Cosine similarity, higher is closer (1.0 identical).")
    distance: float = Field(description="1 - similarity.")
    keyword_score: float = Field(0.0, description="Share of query words found in the chunk (hybrid rerank signal).")
    rank: int
    title: Optional[str] = None
    metadata: Dict[str, Any] = {}

    @property
    def url(self) -> Optional[str]:
        """Source link when the chunk carries one (SEC filings do)."""
        return self.metadata.get("url")


class Metrics(BaseModel):
    retrieval_ms: float = 0.0
    generation_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tokens_estimated: bool = Field(False, description="True when the provider returned no usage and counts are len/4.")
    provider: str = "mock"
    model: str = ""


class RAGResponse(BaseModel):
    answer: str
    chunks: List[RetrievedChunk]
    metrics: Metrics
    warnings: List[str] = []

    @property
    def retrieved_chunks(self) -> List[RetrievedChunk]:
        """Alias of `chunks`, for readability in lab code: print(response.retrieved_chunks)."""
        return self.chunks


class IngestionResult(BaseModel):
    documents: int
    chunks: int
    skipped: Dict[str, str] = {}
    elapsed_ms: float
    embedding_model: str
    vector_store: str
    warnings: List[str] = []
    doc_ids: List[str] = []


class VectorHit(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    similarity: float
    embedding: Optional[List[float]] = None
    title: Optional[str] = None
    metadata: Dict[str, Any] = {}


class EvalResult(BaseModel):
    cases: int
    hits: int
    hit_rate: float
    misses: List[Dict[str, Any]] = []
    passed: Optional[bool] = None
