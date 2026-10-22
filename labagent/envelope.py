"""The result envelope every command returns. This is the contract with the orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Envelope:
    command: str
    mode: str = "apply"           # plan | apply | dry-run
    run_id: Optional[str] = None
    cohort: Optional[str] = None
    status: str = "ok"            # ok | partial | failed
    items: List[Dict[str, Any]] = field(default_factory=list)
    pending: List[Dict[str, Any]] = field(default_factory=list)
    unattributed: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    plan: Optional[Dict[str, Any]] = None
    secrets: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def fail(self, message: str) -> "Envelope":
        self.status = "failed"
        self.error = message
        return self

    def finalize(self) -> "Envelope":
        if self.status == "failed":
            return self
        bad = [i for i in self.items if i.get("state") == "failed"]
        if bad or self.pending:
            self.status = "partial"
        return self

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "partial": 1, "failed": 2}[self.status]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id, "command": self.command, "mode": self.mode,
            "cohort": self.cohort, "status": self.status, "summary": self.summary,
            "pending": self.pending, "items": self.items, "unattributed": self.unattributed,
            "warnings": self.warnings, "plan": self.plan, "secrets": self.secrets,
            "error": self.error,
        }
