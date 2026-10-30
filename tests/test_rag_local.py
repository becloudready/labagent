import asyncio
import json
import urllib.error

import pytest

pytest.importorskip("pydantic")

from labagent.ai.rag import RAGLabClient, RAGSettings, chunk_text  # noqa: E402
from labagent.ai.rag import cli as rag_cli  # noqa: E402
from labagent.ai.rag.adapters import MemoryAdapter, VectorStoreError, make_adapter  # noqa: E402
from labagent.ai.rag.config import load_dotenv  # noqa: E402
from labagent.ai.rag.llm import LLMGateway  # noqa: E402

S = dict(vector_db_type="memory", embedding_model_name="hashing")


def offline(url, headers, body, timeout):
    raise urllib.error.URLError("offline")


def make(tmp_path, **kw):
    rag_cli.init(tmp_path, force=True)
    s = RAGSettings(**{**S, **kw})
    return RAGLabClient(settings=s, llm=LLMGateway(s, transport=offline))


def test_init_creates_project_and_refuses_overwrite(tmp_path, capsys):
    assert rag_cli.init(tmp_path) == 0
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    nb = json.loads((tmp_path / "notebooks" / "01_chunking.ipynb").read_text())
    assert nb["nbformat"] == 4 and any("def chunk_fixed" in "".join(c["source"]) for c in nb["cells"])
    assert len(list((tmp_path / "data" / "sample_docs").iterdir())) == 8
    assert (tmp_path / "src" / "main.py").is_file() and (tmp_path / "README.md").is_file()
    assert len(list((tmp_path / "notebooks").glob("*.ipynb"))) == 9 and not (tmp_path / "solutions").exists()
    assert rag_cli.init(tmp_path) == 2 and "--force" in capsys.readouterr().err


def test_ingest_query_inspect_reset(tmp_path):
    c = make(tmp_path)
    r = c.ingest_documents([tmp_path / "data", tmp_path / "missing.md", tmp_path / ".env"])
    assert r.documents == 7 + (1 if pypdf_available() else 0) and r.chunks >= 2 and set(r.skipped) >= {str(tmp_path / "missing.md"), str(tmp_path / ".env")}
    resp = c.query("how often must secrets be rotated", top_k=2)
    assert resp.chunks[0].doc_id == "security-policy" and len(resp.chunks) == 2 and resp.retrieved_chunks is resp.chunks
    assert 0 <= resp.chunks[0].similarity <= 1 and resp.chunks[0].distance == pytest.approx(1 - resp.chunks[0].similarity, abs=1e-3)
    assert resp.metrics.provider == "mock" and resp.metrics.total_tokens > 0 and resp.metrics.tokens_estimated
    assert "mock answer" in resp.answer
    info = c.inspect_vectors("security-policy")
    assert info["embedding_dim"] == 384 and info["chunks"] >= 1 and len(info["items"][0]["embedding_head"]) == 8
    with pytest.raises(KeyError):
        c.inspect_vectors("nope")
    c.reset()
    with pytest.raises(ValueError, match="ingest_documents"):
        c.query("anything")


def pypdf_available():
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def test_sdk_accepts_vector_db_and_llm_provider_and_single_path(tmp_path, monkeypatch):
    for k in ("GROQ_API_KEY", "VECTOR_DB_TYPE", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    rag_cli.init(tmp_path)
    rag = RAGLabClient(vector_db="memory", llm_provider="groq", settings=RAGSettings(embedding_model_name="hashing"))
    assert rag.settings.vector_db_type == "memory" and rag.settings.llm_provider == "groq"
    assert rag.ingest(str(tmp_path / "data" / "sample_docs")).chunks > 0
    rag.llm = LLMGateway(rag.settings, transport=offline)   # no GROQ_API_KEY and no network: must degrade, not raise
    r = rag.query("What is our enterprise security policy?")
    assert r.metrics.provider == "mock" and any("GROQ" in w or "API key" in w for w in r.warnings)


def test_generated_pdf_is_readable():
    pytest.importorskip("pypdf")
    import io
    from pypdf import PdfReader
    from labagent.ai.rag import samples
    text = PdfReader(io.BytesIO(samples.make_pdf(samples.INCIDENT_LINES))).pages[0].extract_text()
    assert "Incident Response Runbook" in text and "72 hours" in text


def test_pdf_without_pypdf_is_skipped_with_fix(tmp_path, monkeypatch):
    from labagent.ai.rag import client as cl
    monkeypatch.setattr(cl, "_has_pypdf", lambda: False)
    rag_cli.init(tmp_path)
    r = make(tmp_path).ingest(tmp_path / "data")
    assert any("pip install pypdf" in v for v in r.skipped.values()) and r.documents == 7


def test_ide_command_binds_loopback_and_requires_password(tmp_path):
    d = rag_cli.ide_command("docker", tmp_path, 9000, "pw")
    assert "127.0.0.1:9000:8080" in d and "pw" not in " ".join(d) and "PASSWORD" in d
    loc = rag_cli.ide_command("local", tmp_path, 9000, "pw")
    assert loc[:3] == ["code-server", "--bind-addr", "127.0.0.1:9000"] and "password" in loc


def test_ide_print_command_initialises_workspace(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(rag_cli.shutil, "which", lambda n: "/usr/bin/" + n)
    assert rag_cli.ide(tmp_path / "ws", 8080, "local", rag_cli.IMAGE, print_only=True) == 0
    assert (tmp_path / "ws" / ".env").exists() and "code-server" in capsys.readouterr().out


def test_ide_missing_code_server_and_docker_fail_clearly(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(rag_cli.shutil, "which", lambda n: None)
    assert rag_cli.ide(tmp_path, 8080, "local", rag_cli.IMAGE) == 2 and "code-server" in capsys.readouterr().err
    assert rag_cli._pick_mode("auto") == "local"


def test_image_and_devcontainer_pin_what_they_download():
    from pathlib import Path
    root = Path(__file__).parent.parent
    df = (root / "Dockerfile.lab").read_text()
    assert "sha256sum -c" in df and "all-MiniLM-L6-v2" in df and "--auth", "password" in df
    dc = json.loads((root / ".devcontainer" / "rag-lab" / "devcontainer.json").read_text())
    assert (root / ".devcontainer" / "rag-lab" / dc["build"]["dockerfile"]).resolve() == (root / "Dockerfile.lab")


def test_async_ingest_matches_sync(tmp_path):
    a = make(tmp_path)
    r = asyncio.run(a.aingest_documents([tmp_path / "data"]))
    b = make(tmp_path)
    assert r.chunks == b.ingest_documents([tmp_path / "data"]).chunks and a.store.count() == b.store.count()


def test_chunking_bounds_and_overlap():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) > 3 and all(len(c) <= 460 for c in chunks)
    assert chunk_text("x " * 2000, size=300, overlap=0)
    assert chunk_text("") == []


def test_provider_chain_falls_back_with_warning():
    calls = []

    def transport(url, headers, body, timeout):
        calls.append(url)
        if "groq" in url:
            raise urllib.error.URLError("down")
        return {"choices": [{"message": {"content": "from ollama"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

    g = LLMGateway(RAGSettings(groq_api_key="k"), transport=transport)
    r = g.complete("q", "ctx")
    assert r.provider == "ollama" and r.text == "from ollama" and (r.prompt_tokens, r.completion_tokens, r.estimated) == (7, 3, False)
    assert any("groq failed" in w for w in r.warnings) and "groq" in calls[0]


def test_no_keys_and_no_ollama_gives_mock_not_an_exception():
    r = LLMGateway(RAGSettings(), transport=offline).complete("q", "ctx")
    assert r.provider == "mock" and "GROQ_API_KEY" in r.text


def test_forced_provider_without_key_warns():
    r = LLMGateway(RAGSettings(llm_provider="openai"), transport=offline).complete("q", "ctx")
    assert r.provider == "mock" and any("OPENAI_API_KEY" in w or "API key" in w for w in r.warnings)


def test_anthropic_payload_and_usage():
    seen = {}

    def transport(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=body)
        return {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 5, "output_tokens": 2}}

    r = LLMGateway(RAGSettings(llm_provider="anthropic", anthropic_api_key="k"), transport=transport).complete("q", "ctx")
    assert r.text == "hi" and r.prompt_tokens == 5 and seen["url"].endswith("/messages") and seen["headers"]["x-api-key"] == "k"


def test_keys_never_in_repr():
    assert "secret" not in repr(RAGSettings(groq_api_key="secret", qdrant_api_key="secret"))


def test_settings_from_env_and_dotenv(tmp_path, monkeypatch):
    s = RAGSettings.from_env({"VECTOR_DB_TYPE": "qdrant", "GROQ_API_KEY": "g", "EMBEDDING_MODEL_NAME": "x"})
    assert (s.vector_db_type, s.groq_api_key, s.embedding_model_name) == ("qdrant", "g", "x")
    env = tmp_path / ".env"
    env.write_text("# c\nRAG_T_A=1\nRAG_T_B=\nRAG_T_C='quoted'\n")
    monkeypatch.setenv("RAG_T_A", "keep")
    monkeypatch.delenv("RAG_T_C", raising=False)
    assert load_dotenv(env) == {"RAG_T_C": "quoted"}


def test_generated_env_template_parses_to_clean_values(tmp_path, monkeypatch):
    for k in ("VECTOR_DB_TYPE", "EMBEDDING_MODEL_NAME", "GROQ_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    rag_cli.init(tmp_path)
    load_dotenv(tmp_path / ".env")
    s = RAGSettings.from_env()
    assert (s.vector_db_type, s.embedding_model_name, s.groq_api_key) == ("chroma", "all-MiniLM-L6-v2", None)
    monkeypatch.delenv("VECTOR_DB_TYPE")
    monkeypatch.delenv("EMBEDDING_MODEL_NAME")


def test_unknown_store_and_missing_chroma_degrade_clearly():
    with pytest.raises(VectorStoreError, match="unknown VECTOR_DB_TYPE"):
        make_adapter(RAGSettings(vector_db_type="nope"), 8)
    try:
        import chromadb  # noqa: F401
    except ImportError:
        w = []
        assert isinstance(make_adapter(RAGSettings(vector_db_type="chroma"), 8, w), MemoryAdapter) and "labagent[rag-local]" in w[0]


def test_cli_is_wired(tmp_path, capsys):
    from labagent.cli import main
    assert main(["rag", "init", "--dir", str(tmp_path / "p")]) == 0
    assert (tmp_path / "p" / "data" / "sample_docs" / "security_policy.md").exists()


def test_http_client_sends_an_explicit_user_agent(monkeypatch):
    from labagent.ai.rag import llm
    seen = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake(req, timeout):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return R()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    llm.http_json("https://x.example/v1", {"Authorization": "Bearer k"}, {}, 5)
    assert seen["user-agent"].startswith("labagent-rag/") and "python-urllib" not in seen["user-agent"].lower()


def test_http_status_is_reported_not_hidden():
    def blocked(url, headers, body, timeout):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    r = LLMGateway(RAGSettings(llm_provider="groq", groq_api_key="k"), transport=blocked).complete("q", "ctx")
    assert r.provider == "mock" and any("HTTP 403" in w for w in r.warnings)


def test_dotenv_accepts_export_prefix_and_empty_existing_var(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("export RAG_T_X=abc\nRAG_T_Y=def\n")
    monkeypatch.delenv("RAG_T_X", raising=False)
    monkeypatch.setenv("RAG_T_Y", "")
    assert load_dotenv(env) == {"RAG_T_X": "abc", "RAG_T_Y": "def"}
    monkeypatch.delenv("RAG_T_X"); monkeypatch.delenv("RAG_T_Y")


def test_unavailable_model_gives_an_actionable_warning():
    def nope(url, headers, body, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    r = LLMGateway(RAGSettings(llm_provider="groq", groq_api_key="k"), transport=nope).complete("q", "ctx")
    assert any("GROQ_MODEL" in w for w in r.warnings) and RAGSettings().groq_model == "openai/gpt-oss-20b"


ROWS = [
    {"id": "AAPL-2025-1A-1", "title": "Apple Risk", "content": "Apple depends on suppliers in Asia and faces tariff risk on components.",
     "meta": {"ticker": "AAPL", "item": "1A", "fiscal_year": 2025, "url": "https://example.com/aapl"}},
    {"id": "AAPL-2025-7-1", "title": "Apple MD&A", "content": "Apple net sales increased driven by services revenue growth.",
     "meta": {"ticker": "AAPL", "item": "7", "fiscal_year": 2025}},
    {"id": "WMT-2025-1A-1", "title": "Walmart Risk", "content": "Walmart faces competition and supplier risk including tariff exposure.",
     "meta": {"ticker": "WMT", "item": "1A", "fiscal_year": 2025}},
    {"id": "WMT-2025-7-1", "title": "Walmart MD&A", "content": "Walmart comparable sales rose and operating income grew this year.",
     "meta": {"ticker": "WMT", "item": "7", "fiscal_year": 2025}},
]


def stores():
    out = [lambda: MemoryAdapter()]
    try:
        import chromadb  # noqa: F401
        from labagent.ai.rag.adapters import ChromaAdapter
        out.append(lambda: ChromaAdapter(RAGSettings(collection="t" + __import__("uuid").uuid4().hex[:8])))
    except ImportError:
        pass
    try:
        import qdrant_client  # noqa: F401
        from labagent.ai.rag.adapters import QdrantAdapter
        out.append(lambda: QdrantAdapter(RAGSettings(collection="t" + __import__("uuid").uuid4().hex[:8]), 384))
    except ImportError:
        pass
    return out


@pytest.mark.parametrize("make_store", stores())
def test_chunks_filters_titles_and_citations_work_on_every_store(make_store):
    s = RAGSettings(**S)
    rag = RAGLabClient(settings=s, store=make_store(), llm=LLMGateway(s, transport=offline))
    seen = []
    res = rag.ingest_chunks(ROWS, progress=lambda d, t: seen.append((d, t)))
    assert res.chunks == 4 and res.doc_ids == ["AAPL-2025-1A", "AAPL-2025-7", "WMT-2025-1A", "WMT-2025-7"] and seen[-1] == (4, 4)
    only_wmt = rag.retrieve("tariff risk", top_k=4, where={"ticker": "WMT"})
    assert {c.metadata["ticker"] for c in only_wmt} == {"WMT"} and len(only_wmt) == 2
    both = rag.retrieve("tariff risk", top_k=4, where={"ticker": ["AAPL", "WMT"], "item": "1A"})
    assert {c.chunk_id for c in both} == {"AAPL-2025-1A-1", "WMT-2025-1A-1"}
    top = rag.retrieve("supplier tariff risk", top_k=1, where={"ticker": "AAPL", "fiscal_year": 2025})[0]
    assert top.title == "Apple Risk" and top.url == "https://example.com/aapl"       # int filter value matches the stored "2025"
    assert rag.retrieve("anything", where={"ticker": "NOPE"}) == []
    r = rag.query("What tariff risks does Apple have?", where={"ticker": "AAPL"})
    assert all(c.metadata["ticker"] == "AAPL" for c in r.retrieved_chunks)
    assert rag.inspect_vectors("AAPL-2025-1A")["items"][0]["chunk_id"] == "AAPL-2025-1A-1"


def test_keyword_weight_zero_is_pure_vector_and_changes_ranking():
    s = RAGSettings(**S)
    rag = RAGLabClient(settings=s, llm=LLMGateway(s, transport=offline))
    rag.ingest_chunks(ROWS)
    pure = rag.retrieve("tariff", top_k=4, keyword_weight=0.0)
    assert all(abs(c.similarity - (1 - c.distance)) < 1e-3 for c in pure)
    mixed = rag.retrieve("tariff", top_k=4, keyword_weight=0.9)
    assert mixed[0].keyword_score >= pure[-1].keyword_score and mixed[0].keyword_score > 0


def test_evaluate_reports_hit_rate_misses_and_gate():
    s = RAGSettings(**S)
    rag = RAGLabClient(settings=s, llm=LLMGateway(s, transport=offline))
    rag.ingest_chunks(ROWS)
    cases = [{"question": "Apple supplier tariff risk", "expect": {"ticker": "AAPL", "item": "1A"}},
             {"question": "Walmart comparable sales operating income", "expect": {"ticker": "WMT", "item": "7"}},
             {"question": "Chevron oil prices", "expect": {"ticker": "CVX"}}]
    r = rag.evaluate(cases, top_k=2, min_hit_rate=0.6)
    assert (r.cases, r.hits, r.passed) == (3, 2, True) and r.misses[0]["expected"] == {"ticker": "CVX"}
    assert rag.evaluate(cases, top_k=2, min_hit_rate=0.9).passed is False
    assert rag.evaluate(cases, top_k=2, where={"ticker": "AAPL"}).hits >= 1


def test_ingest_chunks_skips_empty_rows_and_accepts_text_key():
    s = RAGSettings(**S)
    rag = RAGLabClient(settings=s, llm=LLMGateway(s, transport=offline))
    res = rag.ingest_chunks([{"id": "a-1", "text": "alpha beta"}, {"id": "b-1", "content": ""}, {"id": "c", "content": "gamma", "doc_id": "custom"}])
    assert res.chunks == 2 and set(res.doc_ids) == {"a", "custom"}


def test_sec_tutorial_notebook_is_valid_and_its_code_compiles():
    import ast
    from labagent.ai.rag import samples
    nb = json.loads(samples.sec_notebook())
    code = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]
    assert len(code) >= 8
    for src in code:
        ast.parse(src)
    assert "notebooks/09_sec_filings_rag.ipynb" in samples.workspace()


def test_rag_dir_expands_tilde(tmp_path, monkeypatch):
    """PowerShell passes "~" through literally; the CLI must expand it itself."""
    import argparse
    from labagent.ai.rag import cli as rag_cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    seen = {}
    monkeypatch.setattr(rag_cli, "init", lambda target, force, solutions: seen.update(t=target) or 0)
    rag_cli.run(argparse.Namespace(rag_cmd="init", dir="~/lab", force=False))
    assert seen["t"] == tmp_path / "lab"
