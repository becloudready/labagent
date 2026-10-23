"""Local, zero-cost RAG lab SDK. Install with: pip install 'labagent[rag-local]'.

Exports load lazily so `labagent rag init` and the base CLI work without pydantic installed.
"""
import importlib

_EXPORTS = {"RAGLabClient": ".client", "chunk_text": ".client", "RAGSettings": ".config",
            "RAGResponse": ".models", "EvalResult": ".models", "IngestionResult": ".models"}
__all__ = sorted(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        return getattr(importlib.import_module(_EXPORTS[name], __name__), name)
    raise AttributeError(name)
