"""Vector store adapters. Add a store by subclassing BaseVectorAdapter and registering it in make_adapter().

Shipped: ChromaAdapter (default, embedded, free), MemoryAdapter (no dependencies), QdrantAdapter (remote or local).
Pinecone and pgvector fit the same four methods. For managed pgvector on Lakebase use the pack instead:
`labagent launch rag-sec-filings`.
"""
import math
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .config import RAGSettings
from .models import VectorHit


class VectorStoreError(RuntimeError):
    pass


Where = Optional[Dict[str, Any]]


def norm_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Stores keep metadata as strings so filters behave the same everywhere (fiscal_year 2025 == "2025")."""
    return {str(k): str(v) for k, v in (meta or {}).items() if v is not None}


def matches(meta: Dict[str, Any], where: Where) -> bool:
    """where = {"ticker": "AAPL"} or {"ticker": ["AAPL", "WMT"]} (any of). Several keys must all match."""
    for k, v in (where or {}).items():
        allowed = [str(x) for x in v] if isinstance(v, (list, tuple, set)) else [str(v)]
        if str(meta.get(k)) not in allowed:
            return False
    return True


class BaseVectorAdapter(ABC):
    name = "base"

    @abstractmethod
    def upsert(self, ids: List[str], embeddings: List[List[float]], documents: List[str], doc_ids: List[str],
               metadatas: Optional[List[Dict[str, Any]]] = None, titles: Optional[List[Optional[str]]] = None) -> None: ...

    @abstractmethod
    def query(self, embedding: List[float], top_k: int, where: Where = None) -> List[VectorHit]:
        """Nearest chunks by cosine similarity, best first. `where` keeps only chunks whose metadata matches."""

    @abstractmethod
    def get_doc(self, doc_id: str) -> List[VectorHit]:
        """Every chunk of one document, with embeddings."""

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def count(self) -> int: ...


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryAdapter(BaseVectorAdapter):
    """Pure Python store. Fine for a few thousand chunks; use it when chromadb is not installed."""
    name = "memory"

    def __init__(self) -> None:
        self._rows: Dict[str, VectorHit] = {}

    def upsert(self, ids, embeddings, documents, doc_ids, metadatas=None, titles=None):
        metadatas = metadatas or [{}] * len(ids)
        titles = titles or [None] * len(ids)
        for i, e, d, doc, m, t in zip(ids, embeddings, documents, doc_ids, metadatas, titles):
            self._rows[i] = VectorHit(chunk_id=i, doc_id=doc, text=d, similarity=0.0, embedding=e, title=t, metadata=norm_meta(m))

    def query(self, embedding, top_k, where=None):
        scored = [r.model_copy(update={"similarity": _cosine(embedding, r.embedding or []), "embedding": None})
                  for r in self._rows.values() if matches(r.metadata, where)]
        return sorted(scored, key=lambda r: r.similarity, reverse=True)[:top_k]

    def get_doc(self, doc_id):
        return [r for r in self._rows.values() if r.doc_id == doc_id]

    def reset(self):
        self._rows.clear()

    def count(self):
        return len(self._rows)


class ChromaAdapter(BaseVectorAdapter):
    """Embedded ChromaDB. In memory unless settings.chroma_path is set."""
    name = "chroma"

    def __init__(self, settings: RAGSettings):
        try:
            import chromadb
        except ImportError as e:
            raise VectorStoreError("chromadb is not installed. Fix: pip install 'labagent[rag-local]'") from e
        self._client = chromadb.PersistentClient(path=settings.chroma_path) if settings.chroma_path else chromadb.EphemeralClient()
        self._collection_name = settings.collection
        self._col = self._open()

    def _open(self):
        return self._client.get_or_create_collection(self._collection_name, metadata={"hnsw:space": "cosine"})

    @staticmethod
    def _where(where: Where):
        conds = [{k: ({"$in": [str(x) for x in v]} if isinstance(v, (list, tuple, set)) else str(v))} for k, v in (where or {}).items()]
        return None if not conds else conds[0] if len(conds) == 1 else {"$and": conds}

    @staticmethod
    def _split(meta: Dict[str, Any]):
        meta = dict(meta)
        return meta.pop("doc_id"), meta.pop("_title", None), meta

    def upsert(self, ids, embeddings, documents, doc_ids, metadatas=None, titles=None):
        metadatas = metadatas or [{}] * len(ids)
        titles = titles or [None] * len(ids)
        metas = [{**norm_meta(m), "doc_id": d, **({"_title": t} if t else {})} for m, d, t in zip(metadatas, doc_ids, titles)]
        self._col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metas)

    def query(self, embedding, top_k, where=None):
        n = self._col.count()
        if n == 0:
            return []
        r = self._col.query(query_embeddings=[embedding], n_results=min(top_k, n), include=["documents", "metadatas", "distances"], where=self._where(where))
        out = []
        for i, d, m, dist in zip(r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0]):
            doc, title, meta = self._split(m)
            out.append(VectorHit(chunk_id=i, doc_id=doc, text=d, similarity=1.0 - dist, title=title, metadata=meta))
        return out

    def get_doc(self, doc_id):
        r = self._col.get(where={"doc_id": doc_id}, include=["documents", "embeddings", "metadatas"])
        embeddings = r["embeddings"] if r["embeddings"] is not None else []
        out = []
        for i, d, e, m in zip(r["ids"], r["documents"], embeddings, r["metadatas"]):
            _, title, meta = self._split(m)
            out.append(VectorHit(chunk_id=i, doc_id=doc_id, text=d, similarity=1.0, embedding=[float(x) for x in e], title=title, metadata=meta))
        return out

    def reset(self):
        self._client.delete_collection(self._collection_name)
        self._col = self._open()

    def count(self):
        return self._col.count()


class QdrantAdapter(BaseVectorAdapter):
    """Qdrant over HTTP (Qdrant Cloud or a local container). With no URL it uses an in-process instance.

    Not exercised against a live server in labagent's tests: treat the first run as a smoke test.
    """
    name = "qdrant"

    def __init__(self, settings: RAGSettings, dim: int):
        try:
            from qdrant_client import QdrantClient
        except ImportError as e:
            raise VectorStoreError("qdrant-client is not installed. Fix: pip install qdrant-client") from e
        self._client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key) if settings.qdrant_url else QdrantClient(":memory:")
        self._collection = settings.collection
        self._dim = dim
        self._ensure()

    def _ensure(self):
        from qdrant_client.models import Distance, VectorParams
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection not in existing:
            self._client.create_collection(self._collection, vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE))

    @staticmethod
    def _pid(chunk_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))  # Qdrant point ids must be UUIDs or ints

    def upsert(self, ids, embeddings, documents, doc_ids, metadatas=None, titles=None):
        from qdrant_client.models import PointStruct
        metadatas = metadatas or [{}] * len(ids)
        titles = titles or [None] * len(ids)
        points = [PointStruct(id=self._pid(i), vector=e, payload={"chunk_id": i, "doc_id": d, "text": t, "title": ti, "meta": norm_meta(m)})
                  for i, e, t, d, m, ti in zip(ids, embeddings, documents, doc_ids, metadatas, titles)]
        self._client.upsert(self._collection, points=points)

    @staticmethod
    def _filter(where: Where):
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue
        if not where:
            return None
        return Filter(must=[FieldCondition(key=f"meta.{k}", match=(MatchAny(any=[str(x) for x in v]) if isinstance(v, (list, tuple, set)) else MatchValue(value=str(v))))
                            for k, v in where.items()])

    @staticmethod
    def _hit(p, similarity=None, embedding=None):
        return VectorHit(chunk_id=p.payload["chunk_id"], doc_id=p.payload["doc_id"], text=p.payload["text"], title=p.payload.get("title"),
                         metadata=p.payload.get("meta") or {}, similarity=float(p.score) if similarity is None else similarity, embedding=embedding)

    def query(self, embedding, top_k, where=None):
        flt = self._filter(where)
        if hasattr(self._client, "query_points"):
            res = self._client.query_points(self._collection, query=embedding, limit=top_k, with_payload=True, query_filter=flt).points
        else:  # older qdrant-client
            res = self._client.search(self._collection, query_vector=embedding, limit=top_k, with_payload=True, query_filter=flt)
        return [self._hit(p) for p in res]

    def get_doc(self, doc_id):
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        points, _ = self._client.scroll(self._collection, scroll_filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
                                        limit=10000, with_payload=True, with_vectors=True)
        return [self._hit(p, similarity=1.0, embedding=list(p.vector)) for p in points]

    def reset(self):
        self._client.delete_collection(self._collection)
        self._ensure()

    def count(self):
        return self._client.count(self._collection, exact=True).count


def make_adapter(settings: RAGSettings, dim: int, warnings: Optional[List[str]] = None) -> BaseVectorAdapter:
    kind = settings.vector_db_type.lower()
    if kind == "memory":
        return MemoryAdapter()
    if kind == "qdrant":
        return QdrantAdapter(settings, dim)
    if kind == "chroma":
        try:
            return ChromaAdapter(settings)
        except VectorStoreError as e:
            if warnings is not None:
                warnings.append(f"{e} Using the in-memory store for now; data is lost when the process exits.")
            return MemoryAdapter()
    raise VectorStoreError(f"unknown VECTOR_DB_TYPE '{settings.vector_db_type}': use chroma, qdrant or memory")
