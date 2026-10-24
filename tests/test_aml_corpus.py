"""AML pack, part 2: regulation parsing, the authored documents and the case files."""
import os
import re
from pathlib import Path

import pytest

from labagent.packs.aml import names as N
from labagent.packs.aml.corpus import documents as D
from labagent.packs.aml.corpus import regs
from labagent.packs.aml.gen import get
from labagent.packs.aml.schema import INSTITUTION

ECFR = b"""<?xml version="1.0"?><DLPSTEXTCLASS><TEXT><BODY><ECFRBRWS><AMDDATE>Oct. 6, 2026 </AMDDATE>
<DIV1 N="1" TYPE="TITLE"><DIV3 N="X" TYPE="CHAPTER"><DIV5 N="1020" TYPE="PART"><HEAD>PART 1020</HEAD>
<DIV8 N="&#167; 1020.320" TYPE="SECTION"><HEAD>&#167; 1020.320   Reports by banks of suspicious transactions.</HEAD>
<P>(a) <I>General.</I> Every bank shall file a report.</P><P>(b) File no later than 30 calendar days after detection.</P></DIV8>
<DIV8 N="&#167; 1020.100" TYPE="SECTION"><HEAD>&#167; 1020.100   Definitions.</HEAD><P>Link to an amendment published at 89 FR 1.</P><P>Bank means a bank.</P></DIV8>
</DIV5><DIV5 N="1021" TYPE="PART"><DIV8 N="&#167; 1021.100" TYPE="SECTION"><HEAD>&#167; 1021.100   Casinos.</HEAD><P>Casino text.</P></DIV8></DIV5>
</DIV3></DIV1></ECFRBRWS></BODY></TEXT></DLPSTEXTCLASS>"""

JUSTICE = b"""<?xml version="1.0"?><Statute xmlns:lims="http://justice.gc.ca/lims" lims:current-date="2026-03-31"><Body>
<Section><MarginalNote>Record keeping</MarginalNote><Label>6</Label><Text>Every person shall keep records.</Text></Section>
<Section><MarginalNote>Reports</MarginalNote><Label>7</Label><Subsection><Label>(1)</Label><Text>Report every transaction where</Text>
<Paragraph><Label>(a)</Label><Text>it is related to an offence;</Text></Paragraph><Paragraph><Label>(b)</Label><Text>it is attempted.</Text></Paragraph></Subsection>
<HistoricalNote><HistoricalNoteSubItem>2000, c. 17</HistoricalNoteSubItem></HistoricalNote></Section></Body></Statute>"""


def test_ecfr_parser_keeps_requested_parts_and_drops_navigation_text():
    secs, as_of = regs.parse_ecfr(ECFR)
    assert as_of == "Oct. 6, 2026" and [s["number"] for s in secs] == ["1020.320", "1020.100"]      # part 1021 is not requested
    assert "Link to an amendment" not in secs[1]["text"] and secs[0]["text"].startswith("(a) General.")
    assert [s["number"] for s in regs.parse_ecfr(ECFR, parts=("1021",))[0]] == ["1021.100"]


def test_justice_parser_labels_subparagraphs_and_skips_history():
    secs = regs.parse_justice(JUSTICE)
    assert [s["number"] for s in secs] == ["6", "7"] and secs[1]["heading"] == "Reports"
    assert "(a) it is related to an offence;" in secs[1]["text"] and "2000, c. 17" not in secs[1]["text"]


def test_chunks_carry_citation_url_and_notice_and_never_span_sections():
    secs, as_of = regs.parse_ecfr(ECFR)
    ch = regs.to_chunks(secs, "us", "31 CFR", lambda n: f"https://www.ecfr.gov/current/title-31/section-{n}", as_of, regs.US_NOTICE, "ecfr-31-cfr-x")
    assert {c["meta"]["citation"] for c in ch} == {"31 CFR 1020.320", "31 CFR 1020.100"}
    assert all("unofficial compilation" in c["meta"]["notice"] and c["meta"]["url"].startswith("https://www.ecfr.gov/") for c in ch)
    assert len({c["id"] for c in ch}) == len(ch)
    ca = regs.to_chunks(regs.parse_justice(JUSTICE), "ca", "PCMLTFA", lambda n: "https://laws-lois.justice.gc.ca/x", "2026-03-31", regs.CA_NOTICE, "justice-laws-ca")
    assert {c["meta"]["citation"] for c in ca} == {"PCMLTFA s. 6", "PCMLTFA s. 7"} and "Open Government Licence - Canada" in ca[0]["meta"]["notice"]


def test_regulation_chunks_fetch_each_file_once_and_cache(tmp_path):
    calls = []

    def http(url):
        calls.append(url)
        return ECFR if "ECFR" in url else JUSTICE
    a = regs.regulation_chunks("both", http=http, cache_dir=tmp_path)
    b = regs.regulation_chunks("both", http=http, cache_dir=tmp_path)
    assert len(calls) == 4 and a == b                                   # one US file and three Canadian files, then all from cache
    with pytest.raises(ValueError):
        regs.regulation_chunks("fr", http=http, cache_dir=tmp_path)


def test_citation_helpers_find_and_check_sections():
    text = "See 31 CFR 1020.320 and PCMLTFA s. 7, PCMLTF STR Regulations s. 9, PCMLTF Regulations s. 132 and 31 CFR 9999.9."
    assert regs.cited_sections(text)["PCMLTFA"] == ["7"]
    assert regs.missing_citations(text) == ["31 CFR 9999.9"]


@pytest.mark.skipif(not os.environ.get("AML_LIVE"), reason="downloads the real regulation files; set AML_LIVE=1")
def test_committed_index_matches_the_live_files():
    assert regs.build_index(regs.regulation_chunks("both"))["sections"] == regs.load_index()["sections"]


# ---- authored documents ---------------------------------------------------------------------------------------------------------

HYPE = re.compile(r"\b(seamless|cutting[- ]edge|revolutionary|game[- ]chang|world[- ]class|unlock|leverage|robust|powerful|supercharge)\w*", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


@pytest.fixture(scope="module")
def tables():
    return get("builtin").generate(seed=42, size="small", jurisdiction="both").tables


@pytest.fixture(scope="module")
def all_docs(tables):
    return D.policy_documents("both") + D.case_documents(tables, "both")


def test_every_regulation_cited_in_our_documents_exists():
    for j in ("ca", "us", "both"):
        for doc in D.policy_documents(j):
            assert regs.missing_citations(D.doc_text(doc)) == [], doc.id


def test_documents_state_the_exact_rules_found_in_the_regulation_text():
    us = " ".join(D.doc_text(d) for d in D.policy_documents("us"))
    ca = " ".join(D.doc_text(d) for d in D.policy_documents("ca"))
    assert "more than $10,000" in us and "30 calendar days" in us and "within 15 days following the day the transaction occurred" in us
    assert "$10,000 or more in cash" in ca and "as soon as practicable" in ca and "within 15 days after the day" in ca
    assert "31 CFR 1020.320" in us and "PCMLTFA s. 7" in ca and "PCMLTF STR Regulations s. 9" in ca
    assert "five years" in us and "at least five years" in ca


def test_writing_rules_hold_for_every_generated_document(all_docs):
    for doc in all_docs:
        text = D.to_markdown(doc)
        assert "—" not in text and "–" not in text, doc.id
        assert not EMOJI.search(text) and not HYPE.search(text), doc.id
        assert N.real_bank_hits(text) == [], doc.id
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(p for s in doc.sections for p in s.paragraphs if " | " not in p)):
            assert len(sentence.split()) <= 45, (doc.id, sentence[:80])


def test_documents_use_only_the_invented_institution():
    for doc in D.policy_documents("both"):
        assert INSTITUTION in D.to_markdown(doc)


def test_jurisdiction_both_produces_a_set_per_country():
    ids = [d.id for d in D.policy_documents("both")]
    assert ids == ["policy-ca", "procedure-ca", "guide-ca", "policy-us", "procedure-us", "guide-us"]


# ---- case files -----------------------------------------------------------------------------------------------------------------

def test_one_case_file_per_alert_with_a_blank_investigator_note(tables):
    cases = D.case_documents(tables, "ca")
    assert [c.meta["alert_id"] for c in cases] == [a["id"] for a in tables["alerts"]]
    assert all("______" in D.to_markdown(c) for c in cases)


def test_case_files_do_not_leak_the_answers(tables):
    cases = {c.meta["alert_id"]: D.to_markdown(c) for c in D.case_documents(tables, "ca")}
    for g in tables["ground_truth"]:
        text = cases[g["alert_id"]]
        assert g["reason"] not in text
        assert g["pattern"] == "none" or g["pattern"] not in text and g["pattern"].replace("_", "-") not in text
        assert not re.search(r"\b(label|ground[_ ]truth)\b", text, re.I)


def test_case_files_contain_every_key_transaction(tables):
    cases = {c.meta["alert_id"]: D.to_markdown(c) for c in D.case_documents(tables, "ca")}
    for g in tables["ground_truth"]:
        for tid in g["key_transactions"].split(";"):
            assert tid in cases[g["alert_id"]], (g["alert_id"], tid)


def test_case_chunks_keep_transactions_as_tables_not_text(tables):
    cases = D.case_documents(tables, "ca")
    chunks = [c for d in cases[:5] for c in D.to_chunks(d)]
    assert chunks and all(c["meta"]["doc_type"] == "case" and c["meta"]["alert_id"] for c in chunks)
    assert not any(re.search(r"T\d{7}", c["content"]) for c in chunks)
    assert {c["meta"]["section"] for c in chunks} == {"summary", "kyc"}


def test_policy_chunks_are_one_per_section_with_metadata():
    doc = D.policy_manual("ca")
    chunks = D.to_chunks(doc)
    assert {c["meta"]["section"] for c in chunks} == {s.id for s in doc.sections}
    assert all(c["meta"]["doc_type"] == "policy" and c["meta"]["source"] == "synthetic" for c in chunks)
    assert len({c["id"] for c in chunks}) == len(chunks)


# ---- corpus assembly and snapshots ----------------------------------------------------------------------------------------------

def test_build_chunks_without_regulations_needs_no_network(tables):
    from labagent.packs.aml.corpus.build import build_chunks
    chunks = build_chunks(tables, "both", with_regulations=False)
    assert {c["meta"]["doc_type"] for c in chunks} == {"policy", "procedure", "guide", "case"}
    assert len({c["id"] for c in chunks}) == len(chunks) and {c["meta"]["source"] for c in chunks} == {"synthetic"}


def test_build_chunks_adds_regulations_for_the_chosen_jurisdiction(tables, tmp_path):
    from labagent.packs.aml.corpus.build import build_chunks
    http = lambda url: ECFR if "ECFR" in url else JUSTICE
    ca = build_chunks(tables, "ca", http=http, cache_dir=tmp_path / "c")
    assert {c["meta"]["jurisdiction"] for c in ca if c["meta"]["doc_type"] == "regulation"} == {"ca"}


def test_markdown_files_are_written_one_per_document(tables, tmp_path):
    from labagent.packs.aml.corpus.build import build_documents, write_markdown
    docs = build_documents(tables, "ca")
    paths = write_markdown(docs, tmp_path)
    assert len(paths) == len(docs) and (tmp_path / "policy-ca.md").read_text().startswith("# Maple Ridge Credit Union AML Policy Manual")


def test_corpus_chunks_package_as_a_snapshot_and_fetch_only_sources_are_refused(tables, tmp_path):
    import json
    from labagent.datasets import DatasetError, build_snapshot
    from labagent.packs.aml.corpus.build import build_chunks
    chunks = build_chunks(tables, "ca", with_regulations=False) + regs.to_chunks(
        regs.parse_justice(JUSTICE), "ca", "PCMLTFA", lambda n: "https://laws-lois.justice.gc.ca/x", "2026-03-31", regs.CA_NOTICE, "justice-laws-ca")
    f = tmp_path / "c.jsonl"
    f.write_text("\n".join(json.dumps(c) for c in chunks))
    m = build_snapshot(f, "aml-corpus", "t1", tmp_path / "out", source="labagent AML pack", built="2026-01-01")
    assert m["sources"] == ["justice-laws-ca", "synthetic"] and m["chunks"] == len(chunks) and m["filings"] == []
    bad = chunks + [{"id": "fintrac-1", "title": "t", "content": "indicator text", "meta": {"source": "fintrac", "doc_type": "guidance"}}]
    f.write_text("\n".join(json.dumps(c) for c in bad))
    with pytest.raises(DatasetError, match="fintrac"):
        build_snapshot(f, "aml-corpus", "t2", tmp_path / "out2")
    assert not list((tmp_path / "out2").glob("*.gz"))          # nothing half-written is left behind
