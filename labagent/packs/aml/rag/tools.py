"""What a learner calls from Python or a notebook after `labagent launch aml`.

    from aml_tools import AmlLab
    lab = AmlLab()                       # run it in the lab folder
    lab.ask("When must a suspicious transaction report be filed?")
    lab.show("AL00007")                  # the evidence bundle for one alert
    lab.check(my_narrative)              # are the transaction IDs and sections I cited real?
    lab.score({"AL00007": "escalate"})   # compare my decisions with the answer key
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..corpus.documents import CaseIndex
from ..schema import TABLES, read_csv
from . import quality as Q
from .bundle import client_retriever, evidence_bundle, render

CONFIG = "config.json"
CHUNKS = "corpus/chunks.jsonl"


def open_client(out_dir: Path, settings_update: Optional[Dict[str, Any]] = None):
    """Open the lab's index. If the index is empty (a fresh checkout, or a memory store), rebuild it from corpus/chunks.jsonl."""
    from ...ai.rag import RAGLabClient
    from ...ai.rag.config import RAGSettings
    cfg = json.loads((out_dir / CONFIG).read_text())
    s = RAGSettings.from_env().model_copy(update={"chroma_path": str(out_dir / "index"), "collection": "aml_lab",
                                                  **({"embedding_model_name": "hashing"} if cfg.get("embedder") == "hashing" else {}),
                                                  **(settings_update or {})})
    client = RAGLabClient(settings=s)
    if client.store.count() == 0:
        rows = [json.loads(l) for l in (out_dir / CHUNKS).read_text().splitlines() if l.strip()]
        client.ingest_chunks(rows)
    return client


class AmlLab:
    def __init__(self, out_dir: Union[str, Path] = ".", client: Any = None):
        self.dir = Path(out_dir)
        self.config = json.loads((self.dir / CONFIG).read_text())
        self.tables = read_csv(self.dir / "tables", only=[n for n in TABLES if n != "ground_truth"])
        key = self.dir / "answer_key"
        if (key / "ground_truth.csv").exists():                 # the answer key is kept apart: open it after you have decided
            self.tables.update(read_csv(key, only=["ground_truth"]))
        self.client = client or open_client(self.dir)
        self._ix = CaseIndex(self.tables)

    def alerts(self) -> List[Dict[str, Any]]:
        """Every alert, without the answers."""
        return list(self.tables["alerts"])

    def ask(self, question: str, top_k: int = 5, **filters: Any):
        """Ask the corpus. Filters narrow the search, for example doc_type='policy' or jurisdiction='ca'."""
        return self.client.query(question, top_k=top_k, where=filters or None)

    def evidence(self, alert_id: str) -> Dict[str, Any]:
        return evidence_bundle(self.tables, alert_id, client_retriever(self.client), self.config["jurisdiction"], self._ix,
                               with_regulations=self.config["regulations"])

    def show(self, alert_id: str) -> None:
        print(render(self.evidence(alert_id)))

    def check(self, text: str) -> Dict[str, Any]:
        """Citation check: every transaction ID, regulation section and 'policy section N' you cite must exist."""
        return Q.citation_validity(text, self.tables)

    def triage(self, alert_id: str) -> Optional[str]:
        """Ask the configured chat model for escalate, monitor or dismiss. Returns None when the model gives no clear answer."""
        return Q.triage_llm(self.client, self.evidence(alert_id))

    def score(self, decisions: Dict[str, str]) -> Dict[str, Any]:
        """Compare your decisions with the answer key. Look at this after you have decided, not before."""
        truth = {g["alert_id"]: g["label"] for g in self.tables["ground_truth"]} if "ground_truth" in self.tables else {}
        known = {k: v for k, v in decisions.items() if k in truth}
        right = sorted(k for k, v in known.items() if truth[k] == v)
        return {"decided": len(known), "agree": len(right), "agreement": round(len(right) / len(known), 3) if known else None,
                "disagree": {k: {"you": v, "key": truth[k]} for k, v in known.items() if truth[k] != v}}
