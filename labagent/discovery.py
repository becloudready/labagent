"""Discovery via the Resource Groups Tagging API, plus attribution and classification."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .config import Config
from .context import Ctx


@dataclass
class Resource:
    arn: str
    service: str
    rtype: str
    rid: str
    region: str
    name: str
    tags: Dict[str, str] = field(default_factory=dict)
    attrs: Dict[str, object] = field(default_factory=dict)   # created, last_used, enabled, attachments, ...

    @property
    def key(self) -> Tuple[str, str]:
        return (self.service, self.rtype)


def parse_arn(arn: str, tags: Optional[Dict[str, str]] = None) -> Resource:
    """arn:partition:service:region:account:resource. Resource part formats vary by service."""
    parts = arn.split(":", 5)
    service, region, res = parts[2], parts[3], parts[5]
    rtype, rid = service, res
    if service == "s3":
        rtype, rid = "bucket", res.split("/")[0]
    elif service == "lambda" and res.startswith("function:"):
        rtype, rid = "function", res.split(":")[1]
    elif service == "dynamodb" and res.startswith("table/"):
        rtype, rid = "table", res.split("/")[1]
    elif service == "logs" and res.startswith("log-group:"):
        rtype, rid = "log-group", res[len("log-group:"):].rstrip(":*")
    elif service == "cloudfront" and res.startswith("distribution/"):
        rtype, rid = "distribution", res.split("/")[1]
    elif service == "apigateway":
        # arn:aws:apigateway:region::/restapis/<id>  or  /apis/<id>
        segs = [s for s in res.split("/") if s]
        if segs and segs[0] == "restapis" and len(segs) == 2:
            rtype, rid = "restapi", segs[1]
        elif segs and segs[0] == "apis" and len(segs) == 2:
            rtype, rid = "httpapi", segs[1]
        else:
            rtype, rid = "other", res
    elif "/" in res:
        rtype, rid = res.split("/", 1)
    elif ":" in res:
        rtype, rid = res.split(":", 1)
    t = tags or {}
    name = t.get("Name") or rid
    return Resource(arn=arn, service=service, rtype=rtype, rid=rid, region=region or "global", name=name, tags=t)


def discover(ctx: Ctx) -> List[Resource]:
    cfg = ctx.cfg
    regions = list(dict.fromkeys(list(cfg.regions) + ["us-east-1"]))  # us-east-1 hosts global services
    seen: Dict[str, Resource] = {}
    for region in regions:
        c = ctx.client("resourcegroupstaggingapi", region)
        pager = c.get_paginator("get_resources")
        for page in pager.paginate(TagFilters=[{"Key": cfg.workshop_tag, "Values": [cfg.workshop]}],
                                   ResourcesPerPage=100):
            for m in page["ResourceTagMappingList"]:
                tags = {t["Key"]: t["Value"] for t in m.get("Tags", [])}
                r = parse_arn(m["ResourceARN"], tags)
                if r.region == "global":
                    r.region = "us-east-1"
                seen[r.arn] = r
    for r in _ec2_extras(ctx, regions):
        seen.setdefault(r.arn, r)
    return sorted(seen.values(), key=lambda r: (r.service, r.rtype, r.arn))


def _ec2_extras(ctx: Ctx, regions: List[str]) -> List[Resource]:
    """EC2 types read directly by tag filter: the Tagging API is eventually consistent and does not
    reliably return every EC2 type (Elastic IPs, volumes, security groups), and leaked EIPs cost money."""
    cfg, out = ctx.cfg, []
    flt = [{"Name": f"tag:{cfg.workshop_tag}", "Values": [cfg.workshop]}]
    for region in regions:
        ec2 = ctx.client("ec2", region)
        acct = ctx.account_id

        def add(rtype, rid, tags):
            t = {x["Key"]: x["Value"] for x in tags or []}
            out.append(parse_arn(f"arn:aws:ec2:{region}:{acct}:{rtype}/{rid}", t))
        for a in ec2.describe_addresses(Filters=flt)["Addresses"]:
            if "AllocationId" in a:
                add("elastic-ip", a["AllocationId"], a.get("Tags"))
        for page in ec2.get_paginator("describe_volumes").paginate(Filters=flt):
            for v in page["Volumes"]:
                add("volume", v["VolumeId"], v.get("Tags"))
        for page in ec2.get_paginator("describe_security_groups").paginate(Filters=flt):
            for g in page["SecurityGroups"]:
                if g["GroupName"] != "default":
                    add("security-group", g["GroupId"], g.get("Tags"))
    return out


@dataclass
class Scope:
    cohort_id: Optional[str] = None
    cohort_date: Optional[str] = None
    all_cohorts: bool = False

    def validate(self) -> None:
        if not (self.cohort_id or self.cohort_date or self.all_cohorts):
            raise ValueError("scope requires a cohort id/date or explicit all_cohorts")


def tag_get(tags: Dict[str, str], key: str) -> Optional[str]:
    lk = key.lower()
    for k, v in tags.items():
        if k.lower() == lk:
            return v
    return None


def classify(res: Resource, cfg: Config, scope: Scope, supported: Iterable[Tuple[str, str]]) -> Tuple[str, Optional[str]]:
    """Return ('delete', None) or ('skip', reason). Skips are always reported, never silent."""
    if (res.service, res.rtype) not in set(supported):
        return "skip", f"unsupported:{res.service}:{res.rtype}"
    if any(res.name.startswith(p) or res.rid.startswith(p) for p in cfg.protected_name_prefixes):
        return "skip", "protected-name"
    if (tag_get(res.tags, cfg.autodelete_tag) or "").strip().lower() == "false":
        return "skip", "autodelete=false"
    if not scope.all_cohorts:
        cohort = tag_get(res.tags, cfg.cohort_tag)
        date = tag_get(res.tags, cfg.date_tag)
        match = (scope.cohort_id and cohort == scope.cohort_id) or (scope.cohort_date and date == scope.cohort_date)
        if not match:
            return "skip", "other-cohort"
    return "delete", None


def attribute(res: Resource, cfg: Config, slugs: Dict[str, str]) -> Optional[str]:
    """Best-effort owner (learner_id). Owner tags win, then a slug match on the resource name at token boundaries."""
    for k in cfg.owner_tag_keys:
        v = tag_get(res.tags, k)
        if v:
            for lid, slug in slugs.items():
                if v == lid or v == slug:
                    return lid
    name = res.name.lower()
    for lid, slug in slugs.items():
        if re.search(rf"(^|[^a-z0-9]){re.escape(slug.lower())}($|[^a-z0-9])", name):
            return lid
    return None


def slugs_from_roster(learners: List[Dict[str, str]]) -> Dict[str, str]:
    out = {}
    for l in learners:
        u = l.get("username", "")
        out[str(l["learner_id"])] = u.split("@")[0]
    return out
