"""FINTRAC guidance, fetched on the instructor's machine at launch and written into the cohort folder. Never committed to the repository
and never put in a snapshot: its terms have not been reviewed for redistribution (see licences.py)."""
from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from typing import Callable, Dict, List, Tuple

BASE = "https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation"
PAGES = {
    "FINTRAC indicators for financial entities": f"{BASE}/indicators-indicateurs/fin_mltf-eng",
    "FINTRAC guidance on reporting suspicious transactions": f"{BASE}/str-dod/str-dod-eng",
}
SKIP = {"script", "style", "nav", "header", "footer", "noscript", "form"}
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "section", "ul", "ol"}


class _Main(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.skip = 0
        self.in_main = 0
        self.saw_main = False

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.in_main += 1
            self.saw_main = True
        if tag in SKIP:
            self.skip += 1
        if tag in BLOCK and not self.skip:
            self.out.append("\n")
        if tag == "li" and not self.skip:
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag == "main" and self.in_main:
            self.in_main -= 1
        if tag in SKIP and self.skip:
            self.skip -= 1
        if tag in BLOCK and not self.skip:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip and (self.in_main or not self.saw_main):
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Main()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(p.out))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def fetch_guidance(http: Callable[[str], bytes]) -> Tuple[Dict[str, str], List[str]]:
    """Returns ({title: text}, [problems]). A page that cannot be fetched is reported, not fatal."""
    out: Dict[str, str] = {}
    problems: List[str] = []
    for title, url in PAGES.items():
        try:
            text = html_to_text(http(url).decode("utf-8", "replace"))
        except Exception as e:      # a guidance page is a bonus: the lab works without it
            problems.append(f"{title}: {type(e).__name__}")
            continue
        if len(text) < 500:
            problems.append(f"{title}: the page had no readable text")
            continue
        out[title] = (f"{title}\n\nSource: {url}\nDownloaded {date.today().isoformat()} by the instructor from the FINTRAC website. "
                      f"FINTRAC owns this text. Check its terms of use before sharing it outside your cohort.\n\n{text}\n")
    return out, problems
