"""The AML guide: writing rules, required headings and numbers that match the generator."""
import re
from pathlib import Path

import pytest

from labagent.packs.aml.gen import get
from labagent.packs.aml.schema import table_counts

ROOT = Path(__file__).resolve().parents[1]
FILES = ["docs/aml.md"]
HYPE = re.compile(r"\b(seamless|cutting[- ]edge|revolutionary|game[- ]chang|world[- ]class|unlock|supercharge|blazing|robust|powerful|best-in-class)\w*", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


@pytest.mark.parametrize("path", FILES)
def test_writing_rules(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert "—" not in text and not EMOJI.search(text) and not HYPE.search(text), path


def test_lab_guide_headings_match_the_search_phrases():
    text = (ROOT / "docs/aml.md").read_text()
    for h in ("Synthetic AML dataset for RAG", "AML transaction monitoring data with labels", "Copilot training data for banking", "FINTRAC STR narrative practice"):
        assert f"## {h}" in text


def test_stated_sizes_match_what_the_generator_makes():
    small = table_counts(get("builtin").generate(seed=42, size="small", jurisdiction="ca").tables)
    assert (small["customers"], small["accounts"], small["alerts"], small["sanctions_hits"]) == (300, 429, 72, 6) and 42_000 <= small["transactions"] <= 42_999
    medium = table_counts(get("builtin").generate(seed=42, size="medium", jurisdiction="ca").tables)
    assert (medium["customers"], medium["accounts"], medium["alerts"]) == (2000, 2889, 552) and 281_000 <= medium["transactions"] <= 281_999


def test_readme_lists_the_pack():
    r = (ROOT / "README.md").read_text()
    assert "| `aml` |" in r and "docs/aml.md" in r
