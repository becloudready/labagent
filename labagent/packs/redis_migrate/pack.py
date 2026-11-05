"""redis-migrate: find out whether a legacy Azure Cache for Redis can be moved to Azure Managed Redis.

Azure Cache for Redis is retiring and Microsoft no longer lets new customers create one. This pack only reads: `launch` checks the
cache (and, if given, the storage account that would hold the export) and reports blockers and warnings. It creates nothing.
Export, import and post-import checks come in later steps. Not Microsoft's tool.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..base import Pack, PackContext, PackError, Param, Step
from ..azure_labs.pack import _LazyAz
from . import facts, rules


class RedisMigrate(Pack):
    id = "redis-migrate"
    title = "Redis Migrate: check a legacy Azure Cache for Redis before moving it to Azure Managed Redis"
    summary = "Check a legacy Azure Cache for Redis before moving it (read only)"
    aliases = ("redis", "azure-redis")
    platforms = ("azure",)
    cost_tier = "free"
    default_name = "check"
    params = [
        Param("source_cache", None, "Name of the Azure Cache for Redis to check. It is only read", required=True),
        Param("source_rg", None, "Its resource group (default: found by name)"),
        Param("storage_account", None, "Storage account that would hold the export. Checked if given"),
        Param("target_region", None, "Region for the new Azure Managed Redis (default: the cache's region)"),
        Param("subscription", None, "Subscription ID or name (default: the one `az login` selected)"),
    ]
    output_keys = ("verdict", "findings", "cache")

    def services(self, profile):
        return {"az": _LazyAz()}

    def _validate(self, ctx: PackContext) -> Dict[str, Any]:
        az = ctx.services["az"]
        if getattr(az, "subscription", None) is None and ctx.params.get("subscription"):
            az.subscription = ctx.params["subscription"]
        az.account()
        got = facts.gather(az, ctx.params)
        findings = rules.assess(got)
        c = got["cache"]
        verdict = "blocked" if any(x["level"] == "blocker" for x in findings) else "warnings" if any(x["level"] == "warning" for x in findings) else "ok"
        ctx.log(f"  {c.get('name')}: {(c.get('sku') or {}).get('name')} {(c.get('sku') or {}).get('family', '')}{(c.get('sku') or {}).get('capacity', '')}, "
                f"Redis {c.get('redisVersion')}, {c.get('location')}")
        for x in sorted(findings, key=lambda x: ("blocker", "warning", "info").index(x["level"])):
            ctx.log(f"  {x['level'].upper():8} {x['code']}: {x['message']}")
        ctx.state.update(verdict=verdict, findings=findings,
                         cache={"name": c.get("name"), "tier": (c.get("sku") or {}).get("name"), "version": c.get("redisVersion"), "location": c.get("location")})
        if verdict == "blocked":
            blockers = [f"{x['code']}: {x['message']}" for x in findings if x["level"] == "blocker"]
            raise PackError(f"blocked by {len(blockers)} check(s): " + "; ".join(blockers))
        return {"verdict": verdict, "findings": findings, "cache": ctx.state["cache"]}

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("validate", "Read the cache and check that it can be exported to Azure Managed Redis", self._validate)]

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        return [Step("destroy", "Nothing to remove: this pack only reads", lambda c: {})]
