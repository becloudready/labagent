"""Run SQL on a Databricks SQL warehouse through the Statement Execution API, using the Databricks CLI login.

The same code path works on Free Edition, AWS and Azure: no cluster, no job, no notebook run, only a warehouse.
`sleep` and `clock` are injectable so tests never wait.
"""
from __future__ import annotations

import time
from typing import Any, Callable, List

from ..base import PackError

RUNNING = {"PENDING", "RUNNING"}


class Sql:
    def __init__(self, dbx: Any, warehouse_id: str, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.dbx = dbx
        self.warehouse_id = warehouse_id
        self._sleep = sleep
        self._clock = clock

    def run(self, statement: str, timeout: int = 1500, label: str = "") -> List[List[Any]]:
        """Run one statement and return its rows (all values as the API gives them: strings). Raises PackError with the reason."""
        r = self.dbx.json("api", "post", "/api/2.0/sql/statements",
                          body={"warehouse_id": self.warehouse_id, "statement": statement, "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"})
        deadline = self._clock() + timeout
        while (r.get("status") or {}).get("state") in RUNNING:
            if self._clock() > deadline:
                raise PackError(f"SQL timed out after {timeout}s{': ' + label if label else ''}")
            self._sleep(3)
            r = self.dbx.json("api", "get", f"/api/2.0/sql/statements/{r['statement_id']}")
        status = r.get("status") or {}
        if status.get("state") != "SUCCEEDED":
            err = (status.get("error") or {}).get("message") or status.get("state") or "unknown error"
            raise PackError(f"SQL failed{' (' + label + ')' if label else ''}: {' '.join(str(err).split())[:300]}")
        return (r.get("result") or {}).get("data_array") or []

    def scalar(self, statement: str) -> Any:
        rows = self.run(statement)
        return rows[0][0] if rows and rows[0] else None
