"""The step-by-step RAG lab notebooks (labagent/ai/rag/lab): structure always, execution when the embedding stack is installed.

Execution runs every code cell of the *solution* notebook (it must pass all its checks) and of the *student* notebook (it must
stop at a failed check, not at a crash), using the mock LLM so no network or key is needed.
"""
import ast
import json
import os

import pytest

pytest.importorskip("pydantic")

from labagent.ai.rag import cli as rag_cli  # noqa: E402
from labagent.ai.rag import lab  # noqa: E402


def names():
    return [p.stem for p in lab.sources()]


def test_eight_lab_notebooks_in_order():
    assert names() == ["01_chunking", "02_embeddings", "03_vector_store", "04_generation", "05_chroma_and_filters",
                       "06_hybrid_search", "07_evaluation", "08_break_it"]


@pytest.mark.parametrize("keep", ["student", "solution"])
def test_notebooks_are_valid_json_and_every_code_cell_parses(keep):
    built = lab.student() if keep == "student" else lab.solution()
    assert len(built) == 8
    for path, content in built.items():
        nb = json.loads(content)
        assert nb["nbformat"] == 4 and len(nb["cells"]) >= 8, path
        assert len({c["id"] for c in nb["cells"]}) == len(nb["cells"])
        for c in nb["cells"]:
            text = "".join(c["source"])
            assert "####" not in text, f"{path}: an unresolved marker"
            if c["cell_type"] == "code":
                ast.parse(text)


def test_student_notebooks_hide_the_solutions_and_keep_the_stubs():
    student, solution = lab.student(), lab.solution()
    for stem in names():
        s, a = student[f"notebooks/{stem}.ipynb"], solution[f"solutions/{stem}.ipynb"]
        todo = lambda nb: any("TODO" in "".join(c["source"]) for c in json.loads(nb)["cells"] if c["cell_type"] == "code")
        assert s != a and todo(s) and not todo(a), stem
    hidden = {"01_chunking": "chunks, step = [], size - overlap", "02_embeddings": "np.dot(a, b) / (np.linalg.norm(a)",
              "03_vector_store": "np.argsort(-scores)", "08_break_it": 'max(newest.get(h["topic"]'}
    text = lambda nb: "\n".join("".join(c["source"]) for c in json.loads(nb)["cells"])
    for stem, secret in hidden.items():
        assert secret not in text(student[f"notebooks/{stem}.ipynb"]), (stem, secret)
        assert secret in text(solution[f"solutions/{stem}.ipynb"]), (stem, secret)


def test_every_exercise_has_a_check_after_it():
    for stem, content in lab.student().items():
        code = ["".join(c["source"]) for c in json.loads(content)["cells"] if c["cell_type"] == "code"]
        assert sum("check(" in c for c in code) >= 1 and any("raise" not in c for c in code), stem


def test_later_notebooks_start_from_working_earlier_code():
    nb = "".join("".join(c["source"]) for c in json.loads(lab.student()["notebooks/03_vector_store.ipynb"])["cells"])
    assert "def chunk_fixed" in nb and "def chunk_paragraphs" in nb and "return chunks" in nb
    nb7 = "".join("".join(c["source"]) for c in json.loads(lab.student()["notebooks/07_evaluation.ipynb"])["cells"])
    assert "class TinyStore" in nb7 and "def keyword_score" in nb7 and "np.argsort(-scores)" in nb7   # reference code, visible to the learner


def test_init_adds_solutions_only_when_asked(tmp_path):
    a, b = tmp_path / "learner", tmp_path / "instructor"
    assert rag_cli.init(a) == 0 and rag_cli.init(b, solutions=True) == 0
    assert not (a / "solutions").exists() and len(list((b / "solutions").glob("*.ipynb"))) == 8
    assert (a / "notebooks" / "08_break_it.ipynb").is_file() and (a / "README.md").read_text().count("0") > 5
    assert ".venv\\Scripts\\activate" in (a / "README.md").read_text()


def test_sample_documents_are_what_the_lab_expects(tmp_path):
    rag_cli.init(tmp_path)
    got = {p.stem for p in (tmp_path / "data" / "sample_docs").iterdir()}
    assert got == {"security_policy", "data_handling", "incident_response", "remote_work_policy", "vendor_risk_management",
                   "ai_usage_guidelines", "backup_and_recovery", "expense_policy"}
    text = " ".join(" ".join(p.read_text().split()) for p in (tmp_path / "data" / "sample_docs").glob("*.md"))
    for phrase in ("SQ-12", "VR-7", "HR-12", "35 days", "four hours", "department head", "every quarter", "never be entered"):
        assert phrase in text, phrase


# ---- execution (needs the embedding model and Chroma; skipped otherwise) -----------------------------------------

def _run(path, keep, workdir):
    ns = {"__name__": "__lab__"}
    here = os.getcwd()
    os.chdir(workdir)
    try:
        for i, (kind, body) in enumerate(lab.cells(path, keep)):
            if kind == "code":
                exec(compile(body, f"{path.stem}[cell {i}]", "exec"), ns)
    finally:
        os.chdir(here)
    return ns


@pytest.fixture
def lab_dir(tmp_path, monkeypatch):
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("chromadb")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for k in ("GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    rag_cli.init(tmp_path)
    (tmp_path / "notebooks").mkdir(exist_ok=True)
    return tmp_path / "notebooks"


@pytest.mark.parametrize("path", lab.sources(), ids=lambda p: p.stem)
def test_solution_notebook_passes_all_its_checks(path, lab_dir):
    _run(path, "solution", lab_dir)


@pytest.mark.parametrize("path", lab.sources(), ids=lambda p: p.stem)
def test_student_notebook_stops_at_a_failed_check_not_a_crash(path, lab_dir):
    with pytest.raises(AssertionError, match="Check failed"):
        _run(path, "student", lab_dir)
