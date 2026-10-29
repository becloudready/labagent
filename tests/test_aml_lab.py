"""The AML build-it-yourself notebooks (labagent/packs/aml/lab): structure always, execution when the embedding stack is installed.

Execution launches a real small lab (no regulation download, in-memory index for the launch), then runs every code cell of each
solution notebook (all checks must pass) and of each student notebook (it must stop at a failed check, not crash).
"""
import ast
import json
import os

import pytest

pytest.importorskip("pydantic")

from labagent.packs.aml import lab  # noqa: E402
from labagent.packs.aml.pack import AmlLab  # noqa: E402
from labagent.packs.base import run_pack  # noqa: E402


def test_four_notebooks_in_order():
    assert [p.stem for p in lab.sources()] == ["01_policy_search", "02_evidence_bundle", "03_citations_and_narrative", "04_triage_and_evaluation"]


@pytest.mark.parametrize("keep", ["student", "solution"])
def test_notebooks_are_valid_and_every_code_cell_parses(keep):
    built = lab.student() if keep == "student" else lab.solution()
    assert len(built) == 4
    for path, content in built.items():
        nb = json.loads(content)
        assert nb["nbformat"] == 4 and len(nb["cells"]) >= 10, path
        for c in nb["cells"]:
            text = "".join(c["source"])
            assert "####" not in text, f"{path}: an unresolved marker"
            if c["cell_type"] == "code":
                ast.parse(text)


def test_student_notebooks_hide_the_solutions_and_keep_the_stubs():
    student, solution = lab.student(), lab.solution()
    todo = lambda nb: any("TODO" in "".join(c["source"]) for c in json.loads(nb)["cells"] if c["cell_type"] == "code")
    text = lambda nb: "\n".join("".join(c["source"]) for c in json.loads(nb)["cells"])
    for stem in ("01_policy_search", "02_evidence_bundle", "03_citations_and_narrative", "04_triage_and_evaluation"):
        s, a = student[f"notebooks/{stem}.ipynb"], solution[f"solutions/{stem}.ipynb"]
        assert s != a and todo(s) and not todo(a), stem
    hidden = {"01_policy_search": "keep = [i for i, c in enumerate(store.chunks)", "02_evidence_bundle": "seen, frontier, visited = {}",
              "03_citations_and_narrative": "unsupported = [t for t in tx if t in known_tx", "04_triage_and_evaluation": "agree = sum(truth[a]"}
    for stem, secret in hidden.items():
        assert secret not in text(student[f"notebooks/{stem}.ipynb"]), (stem, secret)
        assert secret in text(solution[f"solutions/{stem}.ipynb"]), (stem, secret)


def test_later_notebooks_start_from_working_earlier_code():
    nb3 = "".join("".join(c["source"]) for c in json.loads(lab.student()["notebooks/03_citations_and_narrative.ipynb"])["cells"])
    assert "def search" in nb3 and "def transactions_for" in nb3 and "def bundle" in nb3 and "seen, frontier, visited" in nb3


def test_no_notebook_reads_the_answer_key_to_build_a_bundle():
    nb = "".join("".join(c["source"]) for c in json.loads(lab.solution()["solutions/02_evidence_bundle.ipynb"])["cells"])
    assert "leaks_own_answer" in nb and "CANARY" in nb


# ---- execution ------------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def lab_dir(tmp_path_factory):
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("chromadb")
    root = tmp_path_factory.mktemp("amllab")
    mp = pytest.MonkeyPatch()
    mp.setenv("LABAGENT_HOME", str(root / "home"))
    mp.setenv("EMBEDDING_MODEL_NAME", "hashing")
    mp.setenv("LLM_PROVIDER", "mock")
    for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "CHROMA_PATH"):
        mp.delenv(k, raising=False)
    from labagent.ai.rag import RAGLabClient
    from labagent.ai.rag.config import RAGSettings
    client = lambda out: RAGLabClient(vector_db="memory", llm_provider="mock", settings=RAGSettings.from_env())
    env = run_pack(AmlLab(), "launch", "t", {"size": "small", "out": str(root / "lab"), "embedder": "hashing", "regulations": "skip"}, services={"client": client})
    assert env.status == "ok", env.error
    yield root / "lab" / "notebooks"
    mp.undo()


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


@pytest.mark.parametrize("path", lab.sources(), ids=lambda p: p.stem)
def test_solution_notebook_passes_all_its_checks(path, lab_dir):
    _run(path, "solution", lab_dir)


@pytest.mark.parametrize("path", lab.sources(), ids=lambda p: p.stem)
def test_student_notebook_stops_at_a_failed_check_not_a_crash(path, lab_dir):
    with pytest.raises(AssertionError, match="Check failed"):
        _run(path, "student", lab_dir)
