"""Every data file the code reads at runtime must be shipped in the wheel. Source-tree tests cannot see this: a missing
package-data pattern only breaks users who install from PyPI (this happened in 0.2.0: datasets.json was left out)."""
import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
PKG = ROOT / "labagent"
SKIP_PARTS = {"__pycache__", ".terraform"}


def patterns():
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"\[tool\.setuptools\.package-data\]\s*labagent = \[(.*?)\]", text, re.S).group(1)
    return re.findall(r'"([^"]+)"', block)


def test_every_runtime_data_file_is_covered_by_package_data():
    pats = patterns()
    missing = []
    for f in PKG.rglob("*"):
        if not f.is_file() or f.suffix in (".py", ".pyc") or SKIP_PARTS & set(f.parts) or f.name == "ui.json":
            continue
        rel = f.relative_to(PKG).as_posix()
        if not any(fnmatch.fnmatch(rel, p) for p in pats):
            missing.append(rel)
    assert not missing, f"not shipped in the wheel: {missing}"


def test_the_dataset_registry_pin_is_shipped():
    assert any(fnmatch.fnmatch("packs/rag/sources/datasets.json", p) for p in patterns())
