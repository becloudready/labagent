"""Demo source: 25 invented support-policy passages for a fictional retailer. For smoke tests, not for teaching."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ...base import PackContext
from . import Source

HERE = Path(__file__).parent

EVAL = [
    ("How long do refunds take?", "returns"), ("Can I exchange a size?", "returns"),
    ("Is shipping free?", "shipping"), ("How fast is express delivery?", "shipping"),
    ("My package says delivered but is missing", "shipping"), ("What does the warranty cover?", "warranty"),
    ("How do I file a warranty claim?", "warranty"), ("How many points do I earn per dollar?", "loyalty"),
    ("When do loyalty points expire?", "loyalty"), ("I forgot my password", "account"),
    ("Why did my card payment fail?", "payments"), ("Can I get a copy of my personal data?", "privacy"),
]


class Builtin(Source):
    id = "builtin"
    schema = "rag_builtin"

    def chunks(self, ctx: PackContext) -> List[Dict[str, Any]]:
        rows = [json.loads(l) for l in (HERE / "builtin_corpus.jsonl").read_text().splitlines() if l.strip()]
        return [{"id": f"builtin-{r['id']}", "title": r["title"], "content": r["content"], "meta": {"source": r["source"]}}
                for r in rows]

    def eval_set(self) -> List[Dict[str, Any]]:
        return [{"question": q, "expect": {"source": s}} for q, s in EVAL]

    def ui(self, metas):
        cats = sorted({m["source"] for m in metas})
        return {"title": "Retail Support Assistant", "tagline": "Demo knowledge base (invented policies)",
                "heading": "Ask the support knowledge base", "sub": "Fictional retailer policies, for testing the pipeline.",
                "disclaimer": "Demo data: policies are invented.",
                "facets": [{"key": "source", "label": "Topic", "kind": "chips"}],
                "suggestions": [{"question": q, "label": s.title(), "filters": {"source": s}} for q, s in EVAL if s in cats]}
