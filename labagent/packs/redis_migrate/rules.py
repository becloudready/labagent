"""redis-migrate checks: facts about a legacy Azure Cache for Redis in, findings out. Pure functions: no Azure calls, easy to test.

Every rule comes from Microsoft's docs (import/export, retirement FAQ, the Azure Managed Redis migration skill), not from running a
real legacy cache: Microsoft no longer lets new customers create one. `blocker` means export/import cannot work as asked,
`warning` means it works but something does not carry over, `info` is context.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

RETIRES = "2028-09-30"          # Basic, Standard and Premium. Enterprise tiers retire 2027-03-31 (not checked here yet)
CAN_EXPORT = ("premium",)       # import/export is Premium and Enterprise only
IN_PLACE_HINT = "Microsoft's in-place migration (preview) or an empty Azure Managed Redis cache are the options for Basic and Standard"


def norm_region(region: Any) -> str:
    """'Canada Central' and 'canadacentral' compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(region or "").lower())


def _on(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def assess(f: Dict[str, Any]) -> List[Dict[str, str]]:
    """f: cache (az redis show), links, private_endpoints (int or None), storage (az storage account show or None),
    target_region, amr_regions (set of normalized names or None)."""
    out: List[Dict[str, str]] = []

    def add(level: str, code: str, message: str) -> None:
        out.append({"level": level, "code": code, "message": message})

    c = f["cache"]
    conf = c.get("redisConfiguration") or {}
    tier = str((c.get("sku") or {}).get("name", "")).lower()
    sub = str(c.get("id", "")).split("/")[2] if str(c.get("id", "")).count("/") > 2 else ""

    add("info", "retirement", f"Azure Cache for Redis {tier or 'cache'} retires {RETIRES} and is disabled the day after")

    if c.get("provisioningState") != "Succeeded":
        add("blocker", "not-running", f"cache state is {c.get('provisioningState')}, not Succeeded: wait for it to finish first")
    if tier in ("basic", "standard"):
        add("blocker", "no-export", f"{tier.title()} caches cannot export data (Premium and Enterprise only). {IN_PLACE_HINT}")
    elif tier not in CAN_EXPORT:
        add("warning", "unknown-tier", f"tier {tier or '(unknown)'} was not recognised: this check covers Basic, Standard and Premium")

    if c.get("subnetId"):
        add("blocker", "vnet-injection", "the cache is injected into a virtual network: Azure Managed Redis has no VNet injection, only Private Link")
    if c.get("linkedServers") or f.get("links"):
        add("blocker", "geo-replication", "geo-replication is active: unlink the caches before migrating")

    pe = f.get("private_endpoints")
    if pe:
        add("warning", "private-endpoints", f"{pe} private endpoint(s): private endpoint settings are not copied, create them again on the new cache")
    elif pe is None:
        add("info", "private-endpoints", "could not read private endpoints")

    if int(c.get("shardCount") or 0) > 0:
        add("warning", "clustered", f"clustered cache ({c.get('shardCount')} shards): keys are redistributed on import. "
                                      "Azure Managed Redis is clustered by default, so check your client for cross-slot commands")
    else:
        add("warning", "cross-slot", "Azure Managed Redis is clustered by default: commands that touch keys in different slots (some MGET, MULTI, Lua) can fail. "
                                       "The cache cannot tell us whether your app uses them")
    add("warning", "other-databases", "only database 0 is migrated: keys in databases 1 and above are lost. Run INFO keyspace on the cache to see if you have any")

    if str(conf.get("notify-keyspace-events") or "").strip():
        add("warning", "keyspace-notifications", "keyspace notifications are on: Azure Managed Redis does not have them")
    if _on(conf.get("rdb-backup-enabled")) or _on(conf.get("aof-backup-enabled")):
        add("warning", "persistence", "RDB/AOF persistence settings are not copied: set them again on the new cache")
    if _on(c.get("enableNonSslPort")):
        add("warning", "non-tls-port", "the non-TLS port 6379 is on: Azure Managed Redis allows one mode only (TLS on port 10000, set at creation)")
    add("info", "new-endpoint", "the app must switch to the new hostname and port 10000, and use a new access key or Microsoft Entra ID")
    add("info", "writes-lost", "writes after the snapshot are not copied, and import clears the target and makes it unreachable while it runs")

    version = str(c.get("redisVersion") or "")
    major = re.match(r"(\d+)", version)
    if major and int(major.group(1)) <= 7:
        add("info", "rdb-version", f"Redis {version} writes an RDB that loads into Azure Managed Redis (7.x): older into newer is supported")
    elif major:
        add("blocker", "rdb-version", f"Redis {version} is newer than Azure Managed Redis (7.x): its RDB may not load")

    region, target = norm_region(c.get("location")), norm_region(f.get("target_region") or c.get("location"))
    amr = f.get("amr_regions")
    if amr is None:
        add("info", "amr-region", "could not check which regions offer Azure Managed Redis")
    elif target not in amr:
        add("warning", "amr-region", f"Azure Managed Redis is not listed for {f.get('target_region') or c.get('location')}: pick another region (an RDB can cross regions)")
    if target != region:
        add("info", "cross-region", "target region differs from the cache: the storage account for the import must be in the target region")

    _storage(f.get("storage"), c, sub, add)
    return out


def _storage(s: Any, cache: Dict[str, Any], sub: str, add) -> None:
    if not s:
        add("info", "storage", "no storage_account given: storage checks skipped")
        return
    name = s.get("name", "storage account")
    if str(s.get("kind", "")).lower() == "blobstorage":
        add("blocker", "storage-kind", f"{name} is a blob-only account: export needs a general-purpose account (page blobs)")
    if s.get("isHnsEnabled"):
        add("blocker", "storage-adls", f"{name} has a hierarchical namespace (Data Lake Gen2): export to it is not supported")
    if norm_region(s.get("location")) != norm_region(cache.get("location")):
        add("blocker", "storage-region", f"{name} is in {s.get('location')}, the cache is in {cache.get('location')}: export needs the same region")
    s_sub = str(s.get("id", "")).split("/")[2] if str(s.get("id", "")).count("/") > 2 else ""
    if s_sub and sub and s_sub.lower() != sub.lower():
        add("warning", "storage-subscription", f"{name} is in a different subscription: export then needs a managed identity, not a SAS token")
    rules = s.get("networkRuleSet") or {}
    if str(rules.get("defaultAction", "")).lower() == "deny":
        add("warning", "storage-firewall", f"{name} has a firewall: a Premium cache needs 'trusted services' allowed plus a managed identity with "
                                          "Storage Blob Data Contributor. Enterprise tiers do not support a firewall at all")
