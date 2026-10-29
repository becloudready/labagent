"""RAGLabClient: the high-level SDK for the local RAG lab.

    client = RAGLabClient()
    client.ingest_documents(["data/"])
    print(client.query("What is a vector database?").answer)

How it connects to labagent: `labagent rag init` scaffolds a project that uses this client, `labagent rag serve` shows
it in a browser, and the Databricks tutorial (`labagent launch rag-sec-tutorial`) builds the same pipeline on Lakebase.
"""
import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .adapters import BaseVectorAdapter, make_adapter
from .config import RAGSettings
from .embeddings import Embedder, make_embedder
from .llm import LLMGateway
from .adapters import Where, matches
from .models import EvalResult, IngestionResult, Metrics, RAGResponse, RetrievedChunk

TEXT_SUFFIXES = {".txt", ".md", ".rst"}
PDF_SUFFIX = ".pdf"
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an the of to in on for and or is are was be what how why when which who does do it its with as by at from this that".split())
EMBED_BATCH = 64


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> List[str]:
    """Paragraph-aware chunks of about `size` characters; a tail of `overlap` characters carries into the next chunk."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    cur = ""
    for p in paragraphs:
        while len(p) > size:  # a single huge paragraph: cut at a space near the limit
            cut = max(p.rfind(" ", 0, size), size // 2)
            chunks.append((cur + " " + p[:cut + 1]).strip() if cur else p[:cut + 1].strip())
            cur, p = "", p[cut + 1:].lstrip()
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = cur[-overlap:] if overlap else ""
        cur = (cur + "\n\n" + p).strip() if cur else p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


def _slug(path: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-") or "doc"


def _keywords(text: str) -> List[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader  # lazy: only needed for PDFs
    return "\n\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def _has_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def _expand(sources: Sequence[Union[str, Path]]) -> Tuple[List[Path], Dict[str, str]]:
    files: List[Path] = []
    skipped: Dict[str, str] = {}
    pdf_ok = _has_pypdf()
    ok = TEXT_SUFFIXES | ({PDF_SUFFIX} if pdf_ok else set())

    def note_pdf(f: Path) -> None:
        if f.suffix.lower() == PDF_SUFFIX and not pdf_ok:
            skipped[str(f)] = "PDF support needs pypdf: pip install pypdf"

    for s in sources:
        p = Path(s).expanduser()
        if p.is_dir():
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                if f.suffix.lower() in ok:
                    files.append(f)
                else:
                    note_pdf(f)
        elif p.is_file():
            if p.suffix.lower() in ok:
                files.append(p)
            else:
                skipped[str(p)] = (f"unsupported type '{p.suffix}' (supported: {', '.join(sorted(ok))})" if p.suffix.lower() != PDF_SUFFIX
                                   else "PDF support needs pypdf: pip install pypdf")
        else:
            skipped[str(p)] = "not found"
    return files, skipped


class RAGLabClient:
    def __init__(self, vector_db: Optional[str] = None, llm_provider: Optional[str] = None, settings: Optional[RAGSettings] = None,
                 embedder: Optional[Embedder] = None, store: Optional[BaseVectorAdapter] = None, llm: Optional[LLMGateway] = None):
        """vector_db ("chroma" | "qdrant" | "memory") and llm_provider ("groq" | "ollama" | "openai" | "anthropic" | "mock")
        override the environment. Anything left out comes from RAGSettings.from_env(); a missing API key never raises."""
        self.settings = settings or RAGSettings.from_env()
        override = {k: v for k, v in (("vector_db_type", vector_db), ("llm_provider", llm_provider)) if v}
        if override:
            self.settings = self.settings.model_copy(update=override)
        self.warnings: List[str] = []
        self.embedder = embedder or make_embedder(self.settings, self.warnings)
        self.store = store or make_adapter(self.settings, self.embedder.dim, self.warnings)
        self.llm = llm or LLMGateway(self.settings)

    # ---- ingestion -------------------------------------------------------------------------------------------------
    def _read_chunks(self, path: Path) -> Tuple[str, List[str]]:
        text = _pdf_text(path) if path.suffix.lower() == PDF_SUFFIX else path.read_text(errors="replace", encoding="utf-8")
        return _slug(path), chunk_text(text, self.settings.chunk_chars, self.settings.chunk_overlap)

    def _index(self, per_doc: List[Tuple[str, List[str]]]) -> Tuple[int, List[str]]:
        ids: List[str] = []
        texts: List[str] = []
        docs: List[str] = []
        seen: Dict[str, int] = {}
        for doc_id, chunks in per_doc:
            seen[doc_id] = seen.get(doc_id, 0) + 1
            if seen[doc_id] > 1:  # two files with the same stem
                doc_id = f"{doc_id}-{seen[doc_id]}"
            for n, c in enumerate(chunks):
                ids.append(f"{doc_id}::{n}")
                texts.append(c)
                docs.append(doc_id)
        for i in range(0, len(texts), EMBED_BATCH):
            b = slice(i, i + EMBED_BATCH)
            self.store.upsert(ids[b], self.embedder.embed(texts[b]), texts[b], docs[b])
        return len(texts), sorted(set(docs))

    def _result(self, t0: float, n_docs: int, n_chunks: int, skipped: Dict[str, str], doc_ids: List[str]) -> IngestionResult:
        return IngestionResult(documents=n_docs, chunks=n_chunks, skipped=skipped, elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                               embedding_model=self.embedder.name, vector_store=self.store.name, warnings=list(self.warnings), doc_ids=doc_ids)

    def ingest(self, sources: Union[str, Path, Sequence[Union[str, Path]]]) -> IngestionResult:
        """Short form of ingest_documents(): accepts one path or a list."""
        return self.ingest_documents([sources] if isinstance(sources, (str, Path)) else sources)

    def ingest_documents(self, sources: Sequence[Union[str, Path]]) -> IngestionResult:
        """Read .txt/.md/.rst (and .pdf when pypdf is installed) files or folders, chunk, embed and index them. Re-ingesting a file overwrites its chunks; call reset() to start clean."""
        t0 = time.perf_counter()
        files, skipped = _expand(sources)
        n, doc_ids = self._index([self._read_chunks(f) for f in files])
        return self._result(t0, len(files), n, skipped, doc_ids)

    async def aingest_documents(self, sources: Sequence[Union[str, Path]]) -> IngestionResult:
        """Async variant: files are read and chunked concurrently in worker threads; embedding runs off the event loop."""
        t0 = time.perf_counter()
        files, skipped = _expand(sources)
        per_doc = await asyncio.gather(*(asyncio.to_thread(self._read_chunks, f) for f in files))
        n, doc_ids = await asyncio.to_thread(self._index, list(per_doc))
        return self._result(t0, len(files), n, skipped, doc_ids)

    def ingest_chunks(self, rows: Sequence[Dict[str, Any]], progress: Optional[Callable[[int, int], None]] = None) -> IngestionResult:
        """Index pre-chunked rows: {"id", "content" (or "text"), "title"?, "meta"?, "doc_id"?}. Use this for datasets such as the
        SEC 10-K snapshot (labagent.datasets.fetch_snapshot). `meta` becomes filterable. doc_id defaults to the id minus a trailing -N."""
        t0 = time.perf_counter()
        ids, texts, docs, metas, titles = [], [], [], [], []
        for r in rows:
            text = r.get("content") or r.get("text")
            if not text:
                continue
            ids.append(str(r["id"]))
            texts.append(text)
            docs.append(str(r.get("doc_id") or re.sub(r"-\d+$", "", str(r["id"]))))
            metas.append(r.get("meta") or {})
            titles.append(r.get("title"))
        for i in range(0, len(texts), EMBED_BATCH):
            b = slice(i, i + EMBED_BATCH)
            self.store.upsert(ids[b], self.embedder.embed(texts[b]), texts[b], docs[b], metas[b], titles[b])
            if progress:
                progress(min(i + EMBED_BATCH, len(texts)), len(texts))
        return self._result(t0, len(set(docs)), len(texts), {}, sorted(set(docs)))

    # ---- query -----------------------------------------------------------------------------------------------------
    def retrieve(self, prompt: str, top_k: int = 3, where: Where = None, keyword_weight: float = 0.3,
                 candidates: Optional[int] = None) -> List[RetrievedChunk]:
        """Hybrid retrieval. Fetch `candidates` nearest chunks by vector (default 4x top_k), keep those whose metadata matches `where`,
        then rank by (1 - keyword_weight) * similarity + keyword_weight * keyword overlap. keyword_weight=0 is pure vector search."""
        hits = self.store.query(self.embedder.embed([prompt])[0], candidates or max(top_k * 4, top_k), where)
        words = set(_keywords(prompt))
        scored = []
        for h in hits:
            kw = len(words & set(_keywords(h.text + " " + (h.title or "")))) / len(words) if words else 0.0
            scored.append(((1 - keyword_weight) * h.similarity + keyword_weight * kw, kw, h))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [RetrievedChunk(doc_id=h.doc_id, chunk_id=h.chunk_id, text=h.text, similarity=round(h.similarity, 4), distance=round(1 - h.similarity, 4),
                               keyword_score=round(kw, 3), rank=i + 1, title=h.title, metadata=h.metadata) for i, (_, kw, h) in enumerate(scored[:top_k])]

    def evaluate(self, cases: Sequence[Dict[str, Any]], top_k: int = 5, min_hit_rate: Optional[float] = None, **retrieve_kw: Any) -> EvalResult:
        """Retrieval quality check. A case is {"question", "expect": {metadata subset}} and hits when any top_k chunk matches `expect`.
        Extra keyword arguments (where=, keyword_weight=) go to retrieve(), so you can compare settings on the same questions."""
        hits, misses = 0, []
        for c in cases:
            got = self.retrieve(c["question"], top_k, **retrieve_kw)
            if any(matches(g.metadata, c["expect"]) for g in got):
                hits += 1
            else:
                misses.append({"question": c["question"], "expected": c["expect"], "got": [g.chunk_id for g in got]})
        rate = hits / len(cases) if cases else 0.0
        return EvalResult(cases=len(cases), hits=hits, hit_rate=round(rate, 3), misses=misses, passed=None if min_hit_rate is None else rate >= min_hit_rate)

    def query(self, prompt: str, top_k: int = 3, where: Where = None, keyword_weight: float = 0.3) -> RAGResponse:
        if self.store.count() == 0:
            raise ValueError("nothing is indexed yet: call ingest_documents() first (or run `labagent rag init` for sample data)")
        t0 = time.perf_counter()
        chunks = self.retrieve(prompt, top_k, where, keyword_weight)
        t1 = time.perf_counter()
        context = "\n\n".join(f"[{c.doc_id}] {c.title + chr(10) if c.title else ''}{c.text}" for c in chunks)
        res = self.llm.complete(prompt, context)
        t2 = time.perf_counter()
        m = Metrics(retrieval_ms=round((t1 - t0) * 1000, 1), generation_ms=round((t2 - t1) * 1000, 1), prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens, total_tokens=res.prompt_tokens + res.completion_tokens, tokens_estimated=res.estimated,
                    provider=res.provider, model=res.model)
        return RAGResponse(answer=res.text, chunks=chunks, metrics=m, warnings=self.warnings + res.warnings)

    # ---- inspection ------------------------------------------------------------------------------------------------
    def inspect_vectors(self, doc_id: str) -> dict:
        """What the store holds for one document: chunk ids, text and the first 8 numbers of each embedding."""
        rows = self.store.get_doc(doc_id)
        if not rows:
            raise KeyError(f"no document '{doc_id}' (indexed: use result.doc_ids from ingest_documents)")
        return {"doc_id": doc_id, "chunks": len(rows), "embedding_dim": len(rows[0].embedding or []), "embedding_model": self.embedder.name,
                "vector_store": self.store.name,
                "items": [{"chunk_id": r.chunk_id, "text": r.text, "embedding_head": [round(x, 4) for x in (r.embedding or [])[:8]]} for r in rows]}

    def reset(self) -> None:
        self.store.reset()
