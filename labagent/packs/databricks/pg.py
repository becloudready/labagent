"""Postgres access to Lakebase using a short-lived OAuth token (never stored)."""
from __future__ import annotations

from typing import Any

from ..base import PackError


def connect(host: str, user: str, token: str, dbname: str = "databricks_postgres") -> Any:
    try:
        import psycopg
    except ImportError as e:
        raise PackError("psycopg is required: pip install 'labagent[rag]'") from e
    return psycopg.connect(host=host, port=5432, dbname=dbname, user=user, password=token,
                           sslmode="require", autocommit=True, connect_timeout=30)


def find(obj: Any, *keys: str) -> Any:
    """First value found under any of `keys`, searching nested dicts/lists. API field paths differ between
    Lakebase API versions, so lookups are tolerant rather than hard-coded."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = find(v, *keys)
            if r not in (None, ""):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find(v, *keys)
            if r not in (None, ""):
                return r
    return None
