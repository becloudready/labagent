"""Read-only Azure calls that collect the facts `rules.assess` needs. Anything optional fails soft: a missing fact becomes an info line."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..azure_labs.azcli import AzCli
from ..base import PackError
from .rules import norm_region


def _try(fn, default=None):
    try:
        return fn()
    except PackError:
        return default


def find_cache(az: AzCli, name: str, rg: Optional[str]) -> Dict[str, Any]:
    if rg:
        return az.json("redis", "show", "-g", rg, "-n", name)
    hits = [c for c in az.json("redis", "list") if c.get("name") == name]
    if not hits:
        raise PackError(f"no Azure Cache for Redis named {name} in this subscription")
    if len(hits) > 1:
        raise PackError(f"{len(hits)} caches are named {name}: add --param source_rg=<resource group>")
    return hits[0]


def gather(az: AzCli, p: Dict[str, Any]) -> Dict[str, Any]:
    cache = find_cache(az, p["source_cache"], p.get("source_rg"))
    rg = cache.get("resourceGroup") or p.get("source_rg")
    links = _try(lambda: az.json("redis", "server-link", "list", "-g", rg, "-n", cache["name"]), []) or []
    pes = _try(lambda: len(az.json("network", "private-endpoint-connection", "list", "--id", cache["id"])))
    storage = None
    if p.get("storage_account"):
        rows = az.json("storage", "account", "list", "--query", f"[?name=='{p['storage_account']}']")
        if not rows:
            raise PackError(f"storage account {p['storage_account']} was not found in this subscription")
        storage = rows[0]
    regions = _try(lambda: az.json("provider", "show", "-n", "Microsoft.Cache", "--query",
                                   "resourceTypes[?resourceType=='redisEnterprise'].locations[]"))
    return {"cache": cache, "links": links, "private_endpoints": pes, "storage": storage,
            "target_region": p.get("target_region") or cache.get("location"),
            "amr_regions": {norm_region(r) for r in regions} if regions else None}
