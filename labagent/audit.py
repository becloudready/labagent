"""Full-account discovery by listing each service (not by tag), for cleaning up accounts that were never tagged.

Read-only. Complements discovery.py, which finds tagged lab resources through the Tagging API.
"""
from __future__ import annotations

import concurrent.futures as cf
from typing import Callable, Dict, List, Optional

from botocore.exceptions import ClientError, EndpointConnectionError

from .context import Ctx
from .discovery import Resource


def synthetic_arn(service: str, rtype: str, region: str, rid: str) -> str:
    return f"labagent:{service}/{rtype}/{region}/{rid}"


def _tags(raw) -> Dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {str(x.get("Key", x.get("key"))): str(x.get("Value", x.get("value"))) for x in (raw or [])}


def _mk(service, rtype, region, rid, name=None, tags=None, **attrs) -> Resource:
    return Resource(arn=synthetic_arn(service, rtype, region, rid), service=service, rtype=rtype, rid=rid,
                    region=region, name=name or rid, tags=_tags(tags), attrs={k: v for k, v in attrs.items() if v is not None})


def _regional(ctx: Ctx, region: str, out: List[Resource], errors: List[str]) -> None:
    c = lambda s: ctx.client(s, region)

    def lam():
        for p in c("lambda").get_paginator("list_functions").paginate():
            for f in p["Functions"]:
                tg = c("lambda").list_tags(Resource=f["FunctionArn"]).get("Tags", {})
                out.append(_mk("lambda", "function", region, f["FunctionName"], tags=tg, created=f["LastModified"]))

    def apigw():
        for p in c("apigateway").get_paginator("get_rest_apis").paginate():
            for a in p["items"]:
                out.append(_mk("apigateway", "restapi", region, a["id"], a.get("name"), a.get("tags"),
                               created=str(a.get("createdDate"))))
        for a in c("apigatewayv2").get_apis().get("Items", []):
            out.append(_mk("apigateway", "httpapi", region, a["ApiId"], a.get("Name"), a.get("Tags"),
                           created=str(a.get("CreatedDate"))))

    def ddb():
        for p in c("dynamodb").get_paginator("list_tables").paginate():
            for t in p["TableNames"]:
                arn = c("dynamodb").describe_table(TableName=t)["Table"]["TableArn"]
                out.append(_mk("dynamodb", "table", region, t, tags=c("dynamodb").list_tags_of_resource(ResourceArn=arn).get("Tags")))

    def ec2():
        e = c("ec2")
        for p in e.get_paginator("describe_instances").paginate():
            for r in p["Reservations"]:
                for i in r["Instances"]:
                    if i["State"]["Name"] != "terminated":
                        nm = next((x["Value"] for x in i.get("Tags", []) if x["Key"] == "Name"), None)
                        out.append(_mk("ec2", "instance", region, i["InstanceId"], nm, i.get("Tags"),
                                       state=i["State"]["Name"], created=str(i["LaunchTime"])))
        for v in e.describe_volumes()["Volumes"]:
            out.append(_mk("ec2", "volume", region, v["VolumeId"], tags=v.get("Tags"), attached=len(v["Attachments"]) > 0,
                           created=str(v.get("CreateTime"))))
        for a in e.describe_addresses()["Addresses"]:
            out.append(_mk("ec2", "elastic-ip", region, a.get("AllocationId", a.get("PublicIp")), tags=a.get("Tags"),
                           associated="AssociationId" in a))
        for g in e.describe_security_groups()["SecurityGroups"]:
            if g["GroupName"] != "default":
                out.append(_mk("ec2", "security-group", region, g["GroupId"], g["GroupName"], g.get("Tags")))
        for k in e.describe_key_pairs()["KeyPairs"]:
            out.append(_mk("ec2", "key-pair", region, k["KeyName"], tags=k.get("Tags"), created=str(k.get("CreateTime"))))
        for n in e.describe_nat_gateways()["NatGateways"]:
            if n["State"] != "deleted":
                out.append(_mk("ec2", "nat-gateway", region, n["NatGatewayId"], tags=n.get("Tags"), state=n["State"]))
        for v in e.describe_vpcs()["Vpcs"]:
            nm = next((x["Value"] for x in v.get("Tags", []) if x["Key"] == "Name"), None)
            # default VPCs are listed too (so they are visible) but only an explicit rule can remove them
            out.append(_mk("ec2", "vpc", region, v["VpcId"], nm, v.get("Tags"), is_default=bool(v.get("IsDefault")),
                           cidr=v.get("CidrBlock")))

    def elb():
        for lb in c("elbv2").describe_load_balancers()["LoadBalancers"]:
            out.append(_mk("elbv2", "load-balancer", region, lb["LoadBalancerName"], created=str(lb["CreatedTime"])))

    def logs():
        for p in c("logs").get_paginator("describe_log_groups").paginate():
            for g in p["logGroups"]:
                out.append(_mk("logs", "log-group", region, g["logGroupName"], created=str(g.get("creationTime")),
                               bytes=g.get("storedBytes", 0)))

    def ecr():
        for p in c("ecr").get_paginator("describe_repositories").paginate():
            for r in p["repositories"]:
                out.append(_mk("ecr", "repository", region, r["repositoryName"]))

    def cfn():
        for p in c("cloudformation").get_paginator("describe_stacks").paginate():
            for s in p["Stacks"]:
                out.append(_mk("cloudformation", "stack", region, s["StackName"], tags=s.get("Tags"),
                               status=s["StackStatus"], created=str(s.get("CreationTime"))))

    def rds():
        for d in c("rds").describe_db_instances()["DBInstances"]:
            out.append(_mk("rds", "db-instance", region, d["DBInstanceIdentifier"], status=d["DBInstanceStatus"]))

    def eks():
        for a in c("ecs").list_clusters()["clusterArns"]:
            out.append(_mk("ecs", "cluster", region, a.split("/")[-1]))
        for a in c("eks").list_clusters()["clusters"]:
            out.append(_mk("eks", "cluster", region, a))

    def sec():
        for p in c("secretsmanager").get_paginator("list_secrets").paginate():
            for s in p["SecretList"]:
                out.append(_mk("secretsmanager", "secret", region, s["Name"], tags=s.get("Tags")))

    def sns():
        for p in c("sns").get_paginator("list_topics").paginate():
            for t in p["Topics"]:
                out.append(_mk("sns", "topic", region, t["TopicArn"].split(":")[-1]))
        for q in c("sqs").list_queues().get("QueueUrls", []):
            out.append(_mk("sqs", "queue", region, q.split("/")[-1]))

    def layers():
        for p in c("lambda").get_paginator("list_layers").paginate():
            for l in p["Layers"]:
                out.append(_mk("lambda", "layer", region, l["LayerName"], versions=l["LatestMatchingVersion"]["Version"],
                               created=l["LatestMatchingVersion"].get("CreatedDate")))

    def glue():
        g = c("glue")
        for p in g.get_paginator("get_databases").paginate():
            for d in p["DatabaseList"]:
                out.append(_mk("glue", "database", region, d["Name"], created=str(d.get("CreateTime"))))
        for p in g.get_paginator("get_crawlers").paginate():
            for d in p["Crawlers"]:
                out.append(_mk("glue", "crawler", region, d["Name"], state=d.get("State")))
        for p in g.get_paginator("get_jobs").paginate():
            for d in p["Jobs"]:
                out.append(_mk("glue", "job", region, d["Name"], created=str(d.get("CreatedOn"))))

    def athena():
        for w in c("athena").list_work_groups()["WorkGroups"]:
            if w["Name"] != "primary":
                out.append(_mk("athena", "workgroup", region, w["Name"], created=str(w.get("CreationTime"))))

    def alarms():
        for p in c("cloudwatch").get_paginator("describe_alarms").paginate():
            for a in p["MetricAlarms"]:
                out.append(_mk("cloudwatch", "alarm", region, a["AlarmName"], created=str(a.get("AlarmConfigurationUpdatedTimestamp"))))

    for fn in (lam, apigw, ddb, ec2, elb, logs, ecr, cfn, rds, eks, sec, sns, layers, glue, athena, alarms):
        try:
            fn()
        except (ClientError, EndpointConnectionError) as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
            errors.append(f"{region}:{fn.__name__}:{code}")


def _global(ctx: Ctx, out: List[Resource], errors: List[str]) -> None:
    def s3():
        c = ctx.client("s3")
        for b in c.list_buckets()["Buckets"]:
            try:
                tg = c.get_bucket_tagging(Bucket=b["Name"])["TagSet"]
            except ClientError:
                tg = []
            try:
                loc = c.get_bucket_location(Bucket=b["Name"])["LocationConstraint"] or "us-east-1"
            except ClientError:
                loc = ctx.cfg.region
            out.append(_mk("s3", "bucket", loc, b["Name"], tags=tg, created=str(b["CreationDate"])))

    def cloudfront():
        c = ctx.client("cloudfront", "us-east-1")
        for p in c.get_paginator("list_distributions").paginate():
            for d in (p.get("DistributionList", {}).get("Items") or []):
                tg = c.list_tags_for_resource(Resource=d["ARN"])["Tags"]["Items"]
                out.append(_mk("cloudfront", "distribution", "us-east-1", d["Id"], d["DomainName"], tg,
                               enabled=d["Enabled"], status=d["Status"], created=str(d.get("LastModifiedTime")), aliases=len(d.get("Aliases", {}).get("Items", []) or []),
                               comment=d.get("Comment", "")))

    def iam():
        c = ctx.client("iam")
        for p in c.get_paginator("list_users").paginate():
            for u in p["Users"]:
                out.append(_mk("iam", "user", "global", u["UserName"], tags=c.list_user_tags(UserName=u["UserName"]).get("Tags"),
                               created=str(u["CreateDate"]), last_used=str(u["PasswordLastUsed"]) if u.get("PasswordLastUsed") else None))
        for p in c.get_paginator("list_roles").paginate():
            for r in p["Roles"]:
                if not r["Path"].startswith("/aws-service-role/"):
                    out.append(_mk("iam", "role", "global", r["RoleName"], tags=r.get("Tags"), created=str(r["CreateDate"]),
                                   last_used=str(r.get("RoleLastUsed", {}).get("LastUsedDate")) if r.get("RoleLastUsed", {}).get("LastUsedDate") else None))
        for p in c.get_paginator("list_groups").paginate():
            for g in p["Groups"]:
                out.append(_mk("iam", "group", "global", g["GroupName"], created=str(g["CreateDate"])))
        for p in c.get_paginator("list_policies").paginate(Scope="Local"):
            for g in p["Policies"]:
                r = _mk("iam", "policy", "global", g["PolicyName"], created=str(g["CreateDate"]), attachments=g["AttachmentCount"])
                r.arn = g["Arn"]
                out.append(r)

    def dashboards():
        # CloudWatch dashboards are account-wide: list once, always through us-east-1
        cw = ctx.client("cloudwatch", "us-east-1")
        for p in cw.get_paginator("list_dashboards").paginate():
            for d in p["DashboardEntries"]:
                out.append(_mk("cloudwatch", "dashboard", "us-east-1", d["DashboardName"], created=str(d.get("LastModified"))))

    for fn in (s3, cloudfront, iam, dashboards):
        try:
            fn()
        except (ClientError, EndpointConnectionError) as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
            errors.append(f"global:{fn.__name__}:{code}")


def discover_all(ctx: Ctx, regions: Optional[List[str]] = None, workers: int = 12) -> "tuple[List[Resource], List[str]]":
    """Returns (resources, errors). `regions` defaults to config.regions; pass ['*'] for every enabled region."""
    regs = regions or ctx.cfg.regions
    if regs == ["*"]:
        regs = [r["RegionName"] for r in ctx.client("ec2").describe_regions()["Regions"]]
    out: List[Resource] = []
    errors: List[str] = []
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(_regional, ctx, r, out, errors) for r in regs]
        futs.append(ex.submit(_global, ctx, out, errors))
        for f in futs:
            f.result()
    return sorted(out, key=lambda r: (r.service, r.rtype, r.region, r.rid)), sorted(errors)
