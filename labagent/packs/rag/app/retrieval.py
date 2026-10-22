"""Hybrid retrieval shared by the deployed app and the pre-deploy evaluation, so the eval tests the real path.

Vector search (pgvector cosine) and Postgres full-text search each return a candidate pool; reciprocal rank
fusion merges them. Optional metadata filters use jsonb containment (meta @> filter), e.g. {"ticker": "AAPL"}.
"""
import json
import re
from typing import Any, Dict, List, Optional

SCHEMA = re.compile(r"^[a-z_][a-z0-9_]*$")
RRF_K = 60


def vec_literal(v: List[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def search(conn: Any, schema: str, qvec: List[float], qtext: str, k: int = 4, filters: Optional[Dict[str, str]] = None,
           pool: int = 20) -> List[Dict[str, Any]]:
    if not SCHEMA.match(schema):
        raise ValueError("bad schema name")
    where, fargs = ("", [])
    if filters:
        where, fargs = " AND meta @> %s::jsonb", [json.dumps({k_: str(v) for k_, v in filters.items()})]
    lit = vec_literal(qvec)
    vec = conn.execute(
        f"SELECT id, title, content, meta FROM {schema}.chunks WHERE true{where} ORDER BY embedding <=> %s::vector LIMIT %s",
        (*fargs, lit, pool)).fetchall()
    fts = conn.execute(
        f"SELECT id, title, content, meta FROM {schema}.chunks, websearch_to_tsquery('english', %s) q "
        f"WHERE tsv @@ q{where} ORDER BY ts_rank_cd(tsv, q) DESC LIMIT %s", (qtext, *fargs, pool)).fetchall()
    score: Dict[str, float] = {}
    rows: Dict[str, Any] = {}
    for ranked in (vec, fts):
        for rank, r in enumerate(ranked, 1):
            score[r[0]] = score.get(r[0], 0.0) + 1.0 / (RRF_K + rank)
            rows[r[0]] = r
    top = sorted(score, key=score.get, reverse=True)[:k]
    return [{"id": i, "title": rows[i][1], "content": rows[i][2],
             "meta": rows[i][3] if isinstance(rows[i][3], dict) else json.loads(rows[i][3] or "{}"),
             "score": round(score[i], 5)} for i in top]


def is_hit(results: List[Dict[str, Any]], expect: Dict[str, str]) -> bool:
    return any(all(str(r["meta"].get(k)) == str(v) for k, v in expect.items()) for r in results)
