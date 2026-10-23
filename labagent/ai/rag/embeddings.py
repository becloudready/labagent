"""Embedding providers. Default is a local CPU model: no per-token cost, no API key."""
import hashlib
import math
import re
from typing import List, Optional, Protocol

from .config import RAGSettings

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: List[str]) -> List[List[float]]: ...


class SentenceTransformerEmbedder:
    """sentence-transformers on CPU (all-MiniLM-L6-v2, 384 dims). The model downloads once, then runs offline."""

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer  # lazy: heavy import
        self.name = model_name
        self._model = SentenceTransformer(model_name, device="cpu")
        dim_fn = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension  # renamed in newer releases
        self.dim = int(dim_fn())

    def embed(self, texts: List[str]) -> List[List[float]]:
        return self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()


class HashingEmbedder:
    """Dependency-free fallback: hashed bag of words. Matches shared words, NOT meaning ("car" will not match "automobile").

    Used when sentence-transformers is not installed, and in tests. Good enough to run the lab end to end, not to judge quality.
    """

    def __init__(self, dim: int = 384):
        self.name = f"hashing-{dim}"
        self.dim = dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for w in _WORD.findall(text.lower()):
                vec[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def make_embedder(settings: RAGSettings, warnings: Optional[List[str]] = None) -> Embedder:
    if settings.embedding_model_name == "hashing":
        return HashingEmbedder()
    try:
        return SentenceTransformerEmbedder(settings.embedding_model_name)
    except ImportError:
        if warnings is not None:
            warnings.append("sentence-transformers is not installed: using a keyword-only hashing embedder, so results will "
                            "not reflect meaning. Fix: pip install 'labagent[rag-local]'")
        return HashingEmbedder()
