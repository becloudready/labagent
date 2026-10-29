"""Source and builder for the step-by-step RAG lab notebooks.

Each notebook is one text file in this folder, written in the `# %%` cell format. Exercise cells hold both versions:

    #### SOLUTION name
    code the instructor sees
    #### STUDENT
    the stub the learner gets
    #### END

`student()` keeps the stubs, `solution()` keeps the solutions. One source, two notebooks, so they cannot drift apart.
A solution block can be named (`#### SOLUTION name`). A later notebook can then start from the working version of an earlier
exercise with a line `#### REFERENCE name`, which is replaced by that whole solved cell in both notebooks.
"""
import json
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).parent
DOCS = HERE / "docs"


def sources() -> List[Path]:
    return sorted(p for p in HERE.glob("[0-9][0-9]_*.py"))


def _parse(text: str) -> List[Tuple[str, str]]:
    """[(kind, body)] with kind 'markdown' or 'code'."""
    cells: List[Tuple[str, str]] = []
    kind, buf = None, []

    def flush() -> None:
        nonlocal buf
        if kind is not None:
            body = "\n".join(buf).strip("\n")
            if body.strip():
                cells.append((kind, body))
        buf = []

    for line in text.splitlines():
        if line.startswith("# %%"):
            flush()
            kind = "markdown" if "[markdown]" in line else "code"
        elif kind is not None:
            buf.append(line)
    flush()
    return cells


def _blocks(code: str, keep: str, ref: Dict[str, str]) -> str:
    """Resolve SOLUTION/STUDENT blocks and REFERENCE lines. keep is 'solution' or 'student'."""
    out: List[str] = []
    mode = None
    for line in code.splitlines():
        tag = line.strip()
        if tag.startswith("#### SOLUTION"):
            mode = "solution"
        elif tag == "#### STUDENT":
            mode = "student"
        elif tag == "#### END":
            mode = None
        elif tag.startswith("#### REFERENCE "):
            out.append(ref[tag[len("#### REFERENCE "):].strip()])
        elif mode is None or mode == keep:
            out.append(line)
    return "\n".join(out)


def references() -> Dict[str, str]:
    """name -> the whole solved code cell that contains `#### SOLUTION name`, across the notebooks."""
    found: Dict[str, str] = {}
    for path in sources():
        for kind, body in _parse(path.read_text()):
            if kind != "code":
                continue
            for line in body.splitlines():
                tag = line.strip()
                if tag.startswith("#### SOLUTION ") and tag[len("#### SOLUTION "):].strip():
                    found[tag[len("#### SOLUTION "):].strip()] = _blocks(body, "solution", {}).rstrip()
                    break
    return found


def _markdown(body: str) -> str:
    return "\n".join(l[2:] if l.startswith("# ") else l[1:] if l.startswith("#") else l for l in body.splitlines())


def cells(path: Path, keep: str) -> List[Tuple[str, str]]:
    ref = references()
    return [(kind, _markdown(body) if kind == "markdown" else _blocks(body, keep, ref)) for kind, body in _parse(path.read_text())]


def notebook(path: Path, keep: str) -> str:
    nb_cells = []
    for i, (kind, body) in enumerate(cells(path, keep)):
        c = {"cell_type": kind, "metadata": {}, "id": f"cell{i:02d}", "source": body.splitlines(True)}
        if kind == "code":
            c.update({"execution_count": None, "outputs": []})
        nb_cells.append(c)
    nb = {"cells": nb_cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python"}},
          "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb, indent=1) + "\n"


def student() -> Dict[str, str]:
    """notebooks/<name>.ipynb -> content, the learner versions (stubs)."""
    return {f"notebooks/{p.stem}.ipynb": notebook(p, "student") for p in sources()}


def solution() -> Dict[str, str]:
    """solutions/<name>.ipynb -> content, the instructor versions."""
    return {f"solutions/{p.stem}.ipynb": notebook(p, "solution") for p in sources()}


def sample_docs() -> Dict[str, str]:
    """data/sample_docs/<name>.md for the additional documents."""
    return {f"data/sample_docs/{p.name}": p.read_text() for p in sorted(DOCS.glob("*.md"))}
