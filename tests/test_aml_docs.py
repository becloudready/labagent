"""The AML docs, dataset cards and blog draft: writing rules, required headings and numbers that match the generator."""
import re
from pathlib import Path

import pytest

from labagent.packs.aml.gen import get
from labagent.packs.aml.schema import table_counts

ROOT = Path(__file__).resolve().parents[1]
FILES = ["docs/aml.md", "docs/aml/dataset-card-huggingface.md", "docs/aml/kaggle-description.md", "docs/blog/aml-lab.md"]
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


def test_dataset_card_has_the_title_licence_and_credit():
    card = (ROOT / "docs/aml/dataset-card-huggingface.md").read_text()
    assert card.startswith("---\nlicense: cc-by-4.0") and "# Synthetic AML Transactions with Ground Truth (Canada and US)" in card
    kaggle = (ROOT / "docs/aml/kaggle-description.md").read_text()
    for t in (card, kaggle):
        assert "AMLSim" in t and "2306.16424" in t and "github.com/becloudready/labagent" in t.replace("https://", "") or "labagent" in t
    assert "https://github.com/becloudready/labagent" in kaggle and "No IBM" in kaggle


def test_unpublished_things_are_not_claimed_as_published():
    for path in FILES[1:]:
        text = (ROOT / path).read_text().lower()
        assert "not published" in text or "draft" in text, path


def test_stated_sizes_match_what_the_generator_makes():
    small = table_counts(get("builtin").generate(seed=42, size="small", jurisdiction="ca").tables)
    assert (small["customers"], small["accounts"], small["alerts"], small["sanctions_hits"]) == (300, 429, 72, 6) and 42_000 <= small["transactions"] <= 42_999
    medium = table_counts(get("builtin").generate(seed=42, size="medium", jurisdiction="ca").tables)
    assert (medium["customers"], medium["accounts"], medium["alerts"]) == (2000, 2889, 552) and 281_000 <= medium["transactions"] <= 281_999


def test_the_blog_example_is_a_real_alert():
    t = get("builtin").generate(seed=42, size="small", jurisdiction="ca").tables
    blog = (ROOT / "docs/blog/aml-lab.md").read_text()
    tx = {x["id"]: x for x in t["transactions"]}
    g = next(g for g in t["ground_truth"] if g["alert_id"] == "AL00006")
    assert g["label"] == "escalate" and g["pattern"] == "fan_in"
    rows = re.findall(r"\| (T\d{7}) \| \d+ March \| (\S+) \| ([\d,.]+) \|", blog)
    assert len(rows) == 5
    for tid, src, amt in rows:
        assert tx[tid]["from_account"] == src and f"{float(tx[tid]['amount']):,.2f}" == amt and tid in g["key_transactions"]
    assert "17,694.67" in blog and f"{sum(float(tx[t_]['amount']) for t_, *_ in rows):,.2f}" == "17,694.67"


def test_readme_lists_the_pack():
    r = (ROOT / "README.md").read_text()
    assert "| `aml` |" in r and "docs/aml.md" in r
