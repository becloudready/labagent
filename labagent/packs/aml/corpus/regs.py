"""Regulation text for the AML lab: 31 CFR Chapter X (US) and the PCMLTFA with its regulations (Canada).

Both sources are redistributable (US government work; Open Government Licence - Canada), so a pinned snapshot may contain them.
Each chunk carries a notice. The eCFR is an unofficial compilation, and the Justice Laws copy is not the official version of the law.
FINTRAC guidance and the FFIEC manual are NOT handled here: they are fetched on the user's machine and never committed.

Section numbers found in the real files are listed in data/reg_index.json, so the authored documents can be checked offline for
citations that do not exist (`python -m labagent.packs.aml.corpus.regs --write-index` refreshes it; maintainers only).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .... import __version__
from ...rag.chunking import make_chunks

ECFR_URL = "https://www.govinfo.gov/bulkdata/ECFR/title-31/ECFR-title31.xml"
JUSTICE_URL = "https://laws-lois.justice.gc.ca/eng/XML/{code}.xml"
US_PARTS = ("1010", "1020")          # general provisions and rules for banks (credit unions count as banks under 1010.100)
INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "reg_index.json"

US_NOTICE = ("US government work. The eCFR is an unofficial compilation: the official text is in the Federal Register and the Code of "
             "Federal Regulations.")
CA_NOTICE = ("Contains information licensed under the Open Government Licence - Canada. Reproduced from the Justice Laws website of the "
             "Government of Canada. This is not the official version of the law.")

# code -> (short name used in citations, full title, folder on the Justice Laws site)
JUSTICE = {
    "P-24.501": ("PCMLTFA", "Proceeds of Crime (Money Laundering) and Terrorist Financing Act", "acts"),
    "SOR-2002-184": ("PCMLTF Regulations", "Proceeds of Crime (Money Laundering) and Terrorist Financing Regulations", "regulations"),
    "SOR-2001-317": ("PCMLTF STR Regulations", "Proceeds of Crime (Money Laundering) and Terrorist Financing Suspicious Transaction Reporting Regulations", "regulations"),
}

CITE_PATTERNS = {
    "31 CFR": re.compile(r"31 CFR (\d{4}\.\d+)"),
    "PCMLTFA": re.compile(r"PCMLTFA s\. (\d+(?:\.\d+)*)"),
    "PCMLTF Regulations": re.compile(r"PCMLTF Regulations s\. (\d+(?:\.\d+)*)"),
    "PCMLTF STR Regulations": re.compile(r"PCMLTF STR Regulations s\. (\d+(?:\.\d+)*)"),
}


def _clean(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


def parse_ecfr(xml: bytes, parts: Tuple[str, ...] = US_PARTS) -> Tuple[List[Dict[str, Any]], str]:
    """Return (sections, amendment date). A section is {number, heading, text}."""
    root = ET.fromstring(xml)
    as_of = _clean("".join((root.find(".//AMDDATE").itertext()))) if root.find(".//AMDDATE") is not None else ""
    out: List[Dict[str, Any]] = []
    for d3 in root.iter("DIV3"):
        if d3.get("TYPE") == "CHAPTER" and d3.get("N") == "X":
            for d5 in d3.iter("DIV5"):
                if d5.get("N") not in parts:
                    continue
                for d8 in d5.iter("DIV8"):
                    head = _clean("".join(d8.find("HEAD").itertext())) if d8.find("HEAD") is not None else ""
                    m = re.match(r"§\s*(\d{4}\.\d+)\s+(.*)", head)
                    if not m:
                        continue
                    paras = [_clean("".join(p.itertext())) for p in d8.iter("P")]
                    paras = [p for p in paras if p and not p.startswith("Link to an amendment")]
                    if paras:
                        out.append({"number": m.group(1), "heading": m.group(2).rstrip("."), "text": "\n\n".join(paras)})
    return out, as_of


_LABELLED = {"Subsection", "Paragraph", "Subparagraph", "Clause", "Subclause", "Definition"}


def _walk(e: ET.Element, out: List[str]) -> None:
    tag = e.tag.split("}")[-1]
    if tag in ("MarginalNote", "HistoricalNote", "Footnote", "FootnoteRef", "RecentAmendments", "Label"):
        return
    if tag in _LABELLED:
        out.append("\n\n")
        lab = next((c for c in e if c.tag.split("}")[-1] == "Label"), None)
        if lab is not None and lab.text:
            out.append(lab.text + " ")
    if e.text and tag != "Label":
        out.append(e.text)
    for c in e:
        _walk(c, out)
        if c.tail:
            out.append(c.tail)


def parse_justice(xml: bytes) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml)
    body = next((e for e in root.iter() if e.tag.split("}")[-1] == "Body"), root)
    out = []
    for s in body.iter():
        if s.tag.split("}")[-1] != "Section":
            continue
        label = next((c for c in s if c.tag.split("}")[-1] == "Label"), None)
        note = next((c for c in s if c.tag.split("}")[-1] == "MarginalNote"), None)
        if label is None or not label.text:
            continue
        parts: List[str] = []
        _walk(s, parts)
        text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(parts))).strip()
        if text:
            out.append({"number": label.text.strip(), "heading": _clean("".join(note.itertext())) if note is not None else "", "text": text})
    return out


def to_chunks(sections: List[Dict[str, Any]], jurisdiction: str, instrument: str, url_for: Callable[[str], str], as_of: str,
              notice: str, source: str, max_chars: int = 1200) -> List[Dict[str, Any]]:
    """One or more chunks per section; a chunk never spans two sections."""
    slug = re.sub(r"[^a-z0-9]+", "-", instrument.lower()).strip("-")
    chunks: List[Dict[str, Any]] = []
    for s in sections:
        cite = f"{instrument} {s['number']}" if instrument == "31 CFR" else f"{instrument} s. {s['number']}"
        meta = {"doc_type": "regulation", "source": source, "jurisdiction": jurisdiction, "instrument": instrument, "section": s["number"],
                "citation": cite, "heading": s["heading"], "url": url_for(s["number"]), "as_of": as_of, "notice": notice}
        title = f"{cite} {s['heading']}".strip()
        chunks += make_chunks(f"reg-{jurisdiction}-{slug}-{s['number']}", title, s["text"], meta, max_chars=max_chars)
    return chunks


def default_http(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"labagent/{__version__}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def _cached(url: str, name: str, http: Callable[[str], bytes], cache_dir: Optional[Path]) -> bytes:
    root = cache_dir or Path(os.environ.get("LABAGENT_HOME") or Path.home() / ".labagent") / "cache" / "regs"
    path = root / name
    if path.exists():
        return path.read_bytes()
    data = http(url)
    root.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def us_chunks(http: Callable[[str], bytes] = default_http, cache_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    sections, as_of = parse_ecfr(_cached(ECFR_URL, "ECFR-title31.xml", http, cache_dir))
    return to_chunks(sections, "us", "31 CFR", lambda n: f"https://www.ecfr.gov/current/title-31/section-{n}", as_of, US_NOTICE, "ecfr-31-cfr-x")


def ca_chunks(http: Callable[[str], bytes] = default_http, cache_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for code, (short, _title, folder) in JUSTICE.items():
        data = _cached(JUSTICE_URL.format(code=code), f"{code}.xml", http, cache_dir)
        stamp = re.search(rb'lims:current-date="([\d-]+)"', data)
        as_of = stamp.group(1).decode() if stamp else ""
        out += to_chunks(parse_justice(data), "ca", short, lambda n, c=code, f=folder: f"https://laws-lois.justice.gc.ca/eng/{f}/{c}/FullText.html",
                         as_of, CA_NOTICE, "justice-laws-ca")
    return out


def regulation_chunks(jurisdiction: str, http: Callable[[str], bytes] = default_http, cache_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    if jurisdiction not in ("ca", "us", "both"):
        raise ValueError("jurisdiction must be ca, us or both")
    out: List[Dict[str, Any]] = []
    if jurisdiction in ("us", "both"):
        out += us_chunks(http, cache_dir)
    if jurisdiction in ("ca", "both"):
        out += ca_chunks(http, cache_dir)
    return out


def build_index(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    idx: Dict[str, List[str]] = {}
    as_of: Dict[str, str] = {}
    for c in chunks:
        m = c["meta"]
        if m["section"] not in idx.setdefault(m["instrument"], []):
            idx[m["instrument"]].append(m["section"])
        as_of[m["instrument"]] = m["as_of"]
    return {"as_of": as_of, "sections": {k: sorted(v) for k, v in sorted(idx.items())}}


def load_index() -> Dict[str, Any]:
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def cited_sections(text: str) -> Dict[str, List[str]]:
    """Every regulation section a piece of text cites, by instrument."""
    out: Dict[str, List[str]] = {}
    for inst, pat in CITE_PATTERNS.items():
        found = pat.findall(text)
        if found:
            out[inst] = sorted(set(found))
    return out


def missing_citations(text: str, index: Optional[Dict[str, Any]] = None) -> List[str]:
    index = index or load_index()
    return [f"{inst} {n}" for inst, nums in cited_sections(text).items() for n in nums if n not in index["sections"].get(inst, [])]


if __name__ == "__main__":      # maintainers only: refresh the index, or write the chunks for `labagent dataset --chunks`
    import sys
    args = sys.argv[1:]
    if "--write-index" in args:
        index = build_index(regulation_chunks("both"))
        INDEX_PATH.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print({k: len(v) for k, v in index["sections"].items()}, index["as_of"])
    elif "--write-chunks" in args:
        path = Path(args[args.index("--write-chunks") + 1])
        jur = args[args.index("--jurisdiction") + 1] if "--jurisdiction" in args else "both"
        rows = regulation_chunks(jur)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
        print(f"{len(rows)} chunks -> {path}")
    else:
        raise SystemExit("usage: python -m labagent.packs.aml.corpus.regs (--write-index | --write-chunks FILE [--jurisdiction ca|us|both])")
