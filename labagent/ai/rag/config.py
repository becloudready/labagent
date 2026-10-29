"""Settings for the local RAG lab, read from environment variables (and an optional .env file).

Connects to the labagent ecosystem: the Databricks packs (`labagent launch rag-*`) deploy RAG to a managed Lakebase
database. This module is the free, laptop-only sibling: same ideas (chunk, embed, retrieve, answer) with an embedded
vector store, so a learner can experiment before touching any cloud account.
"""
import os
from pathlib import Path
from typing import Dict, Optional

from pydantic import BaseModel, Field

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
GROQ_URL = "https://api.groq.com/openai/v1"
OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1"
OLLAMA_URL = "http://localhost:11434/v1"


def load_dotenv(path: Path) -> Dict[str, str]:
    """Load KEY=VALUE lines into os.environ without overriding variables that are already set. Returns what was set."""
    set_now: Dict[str, str] = {}
    if not path.is_file():
        return set_now
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()  # inline comment on an unquoted value
        if key and value and not os.environ.get(key):  # an existing empty variable counts as unset
            os.environ[key] = value
            set_now[key] = value
    return set_now


class RAGSettings(BaseModel):
    """All knobs for RAGLabClient. Build with RAGSettings.from_env(); API keys never appear in repr or logs."""

    vector_db_type: str = Field("chroma", description="chroma | qdrant | memory")
    embedding_model_name: str = DEFAULT_EMBEDDING_MODEL
    chroma_path: Optional[str] = Field(None, description="Persist Chroma here. Unset keeps everything in memory.")
    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = Field(None, repr=False)
    collection: str = "labagent_rag"
    chunk_chars: int = 800
    chunk_overlap: int = 100
    llm_provider: Optional[str] = Field(None, description="groq | ollama | openai | anthropic | mock. Unset picks the first usable.")
    groq_api_key: Optional[str] = Field(None, repr=False)
    groq_model: str = "openai/gpt-oss-20b"
    openai_api_key: Optional[str] = Field(None, repr=False)
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: Optional[str] = Field(None, repr=False)
    anthropic_model: str = "claude-haiku-4-5-20251001"
    ollama_url: str = OLLAMA_URL
    ollama_model: str = "qwen2.5-coder:0.5b"
    llm_timeout_s: float = 60.0

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "RAGSettings":
        e = os.environ if env is None else env

        def g(name: str) -> Optional[str]:
            return e.get(name) or None

        values = {
            "vector_db_type": g("VECTOR_DB_TYPE"), "embedding_model_name": g("EMBEDDING_MODEL_NAME"),
            "chroma_path": g("CHROMA_PATH"), "qdrant_url": g("QDRANT_URL"), "qdrant_api_key": g("QDRANT_API_KEY"),
            "collection": g("RAG_COLLECTION"), "llm_provider": g("LLM_PROVIDER"),
            "groq_api_key": g("GROQ_API_KEY"), "groq_model": g("GROQ_MODEL"),
            "openai_api_key": g("OPENAI_API_KEY"), "openai_model": g("OPENAI_MODEL"),
            "anthropic_api_key": g("ANTHROPIC_API_KEY"), "anthropic_model": g("ANTHROPIC_MODEL"),
            "ollama_url": g("OLLAMA_URL"), "ollama_model": g("OLLAMA_MODEL"),
        }
        return cls(**{k: v for k, v in values.items() if v is not None})
