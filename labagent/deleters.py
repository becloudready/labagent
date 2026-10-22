"""One deleter per resource type. Each returns a Result and is safe to re-run (idempotent)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from botocore.exceptions import ClientError

from .context import Ctx
from .discovery import Resource


@dataclass
class Result:
    state: str                      # deleted | pending | throttled | failed | gone
    reason: Optional[str] = None
    retry_after_s: Optional[int] = None


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


NOT_FOUND = {"EntityNotFoundException", "DashboardNotFoundError", "ResourceNotFound", "InvalidVpcID.NotFound", "NotFound", "AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist", "RepositoryNotFoundException",
             "InvalidKeyPair.NotFound", "ResourceNotFoundException", "NotFoundException", "NoSuchBucket", "NoSuchDistribution",
             "NoSuchEntity", "InvalidInstanceID.NotFound", "InvalidAllocationID.NotFound",
             "InvalidGroup.NotFound", "InvalidVolume.NotFound"}


def guarded(fn: Callable[[Ctx, Resource], Result]) -> Callable[[Ctx, Resource], Result]:
    def wrapper(ctx: Ctx, r: Resource) -> Result:
        try:
            return fn(ctx, r)
        except ClientError as e:
            c = _code(e)
            if c in NOT_FOUND:
                return Result("gone")
            if c in ("TooManyRequestsException", "Throttling", "ThrottlingException"):
                return Result("throttled", c, 31)
            if c in ("DependencyViolation", "VolumeInUse", "ResourceInUseException", "PreconditionFailed",
                     "DistributionNotDisabled", "DeleteConflict"):
                return Result("pending", c, 300)
            return Result("failed", f"{c}: {e.response['Error'].get('Message', '')[:120]}")
    return wrapper


@guarded
def del_lambda(ctx, r):
    ctx.client("lambda", r.region).delete_function(FunctionName=r.rid)
    return Result("deleted")


@guarded
def del_bucket(ctx, r):
    s3 = ctx.session.resource("s3")
    b = s3.Bucket(r.rid)
    b.object_versions.delete()
    b.objects.all().delete()
    b.delete()
    return Result("deleted")


@guarded
def del_table(ctx, r):
    ctx.client("dynamodb", r.region).delete_table(TableName=r.rid)
    return Result("deleted")


@guarded
def del_loggroup(ctx, r):
    ctx.client("logs", r.region).delete_log_group(logGroupName=r.rid)
    return Result("deleted")


@guarded
def del_instance(ctx, r):
    ctx.client("ec2", r.region).terminate_instances(InstanceIds=[r.rid])
    return Result("deleted")


@guarded
def del_eip(ctx, r):
    ec2 = ctx.client("ec2", r.region)
    addr = ec2.describe_addresses(AllocationIds=[r.rid])["Addresses"][0]
    if "AssociationId" in addr:
        ec2.disassociate_address(AssociationId=addr["AssociationId"])
    ec2.release_address(AllocationId=r.rid)
    return Result("deleted")


@guarded
def del_sg(ctx, r):
    """Groups slated for deletion often reference each other (A allows B, B allows A), which blocks both. Remove this
    group's own rules that point at other groups first (it is being deleted anyway), then delete. A group that is
    still referenced by a not-yet-deleted peer returns pending and is finished by the next run."""
    ec2 = ctx.client("ec2", r.region)
    try:
        g = ec2.describe_security_groups(GroupIds=[r.rid])["SecurityGroups"][0]
    except ClientError as e:
        if _code(e) == "InvalidGroup.NotFound":
            return Result("gone")
        raise
    for key, revoke in (("IpPermissions", ec2.revoke_security_group_ingress), ("IpPermissionsEgress", ec2.revoke_security_group_egress)):
        refs = [{**{k: v for k, v in p.items() if k in ("IpProtocol", "FromPort", "ToPort")},
                 "UserIdGroupPairs": [{"GroupId": x["GroupId"]} for x in p["UserIdGroupPairs"]]}
                for p in g.get(key, []) if p.get("UserIdGroupPairs")]
        if refs:
            revoke(GroupId=r.rid, IpPermissions=refs)
    ec2.delete_security_group(GroupId=r.rid)
    return Result("deleted")


@guarded
def del_volume(ctx, r):
    ctx.client("ec2", r.region).delete_volume(VolumeId=r.rid)
    return Result("deleted")


@guarded
def del_restapi(ctx, r):
    ctx.client("apigateway", r.region).delete_rest_api(restApiId=r.rid)
    return Result("deleted")


@guarded
def del_httpapi(ctx, r):
    ctx.client("apigatewayv2", r.region).delete_api(ApiId=r.rid)
    return Result("deleted")


def _s3_bucket_of(domain: str):
    """Bucket name for a bucket-style origin domain, else None (<bucket>.s3.<region>.amazonaws.com, .s3.amazonaws.com)."""
    m = re.match(r"^(.+?)\.s3[.-]", domain)
    return m.group(1) if m and domain.endswith(".amazonaws.com") else None


def _neutralize_missing_s3_origins(ctx, cfg) -> int:
    """CloudFront refuses to update a distribution whose S3 origin bucket was deleted ("The parameter S3 bucket has
    been deleted"), so it cannot even be disabled. Repoint such origins at a placeholder custom origin; the
    distribution is about to be deleted anyway. Only origins whose bucket is confirmed missing are touched."""
    s3, fixed = ctx.client("s3"), 0
    for o in cfg.get("Origins", {}).get("Items", []):
        bucket = _s3_bucket_of(o["DomainName"])
        if not bucket:
            continue
        try:
            s3.head_bucket(Bucket=bucket)
            continue
        except ClientError as e:
            if _code(e) not in ("404", "NoSuchBucket", "NotFound"):
                continue                       # exists but not ours / no permission: leave alone
        o["DomainName"] = "example.com"
        o.pop("S3OriginConfig", None)
        o["OriginAccessControlId"] = ""
        o["CustomOriginConfig"] = {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
                                   "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                                   "OriginReadTimeout": 30, "OriginKeepaliveTimeout": 5}
        fixed += 1
    return fixed


@guarded
def del_distribution(ctx, r):
    """Two phase: disable now, delete on a later run once Deployed (15 to 30 minutes)."""
    cf = ctx.client("cloudfront", "us-east-1")
    cur = cf.get_distribution_config(Id=r.rid)
    cfg, etag = cur["DistributionConfig"], cur["ETag"]
    if cfg.get("Enabled"):
        cfg["Enabled"] = False
        try:
            cf.update_distribution(Id=r.rid, IfMatch=etag, DistributionConfig=cfg)
        except ClientError as e:
            msg = e.response["Error"].get("Message", "")
            if _code(e) == "InvalidArgument" and "S3 bucket" in msg and "deleted" in msg \
                    and _neutralize_missing_s3_origins(ctx, cfg):
                cf.update_distribution(Id=r.rid, IfMatch=etag, DistributionConfig=cfg)
            else:
                raise
        return Result("pending", "disabling", 900)
    status = cf.get_distribution(Id=r.rid)["Distribution"]["Status"]
    if status != "Deployed":
        return Result("pending", f"status={status}", 600)
    cf.delete_distribution(Id=r.rid, IfMatch=etag)
    return Result("deleted")


def _vpc_blockers(ec2, elbv2, vpc_id: str) -> str:
    """Anything that makes deleting a VPC unsafe or impossible. Empty string = clear to delete."""
    f = [{"Name": "vpc-id", "Values": [vpc_id]}]
    insts = [i for r in ec2.describe_instances(Filters=f)["Reservations"] for i in r["Instances"]
             if i["State"]["Name"] != "terminated"]
    if insts:
        return f"{len(insts)} EC2 instance(s)"
    nats = [n for n in ec2.describe_nat_gateways(Filter=f)["NatGateways"] if n["State"] != "deleted"]
    if nats:
        return f"{len(nats)} NAT gateway(s)"
    lbs = [lb for lb in elbv2.describe_load_balancers()["LoadBalancers"] if lb.get("VpcId") == vpc_id]
    if lbs:
        return f"{len(lbs)} load balancer(s)"
    peer = ec2.describe_vpc_peering_connections(Filters=[{"Name": "requester-vpc-info.vpc-id", "Values": [vpc_id]}])["VpcPeeringConnections"]
    if [p for p in peer if p["Status"]["Code"] not in ("deleted", "rejected", "failed", "expired")]:
        return "VPC peering connection(s)"
    # interface endpoints own ENIs that disappear once the endpoint goes, so only count other ENIs
    enis = [n for n in ec2.describe_network_interfaces(Filters=f)["NetworkInterfaces"]
            if n.get("InterfaceType") != "vpc_endpoint"]
    if enis:
        kinds = sorted({(n.get("Description") or n.get("InterfaceType") or "eni")[:40] for n in enis})
        return f"{len(enis)} network interface(s) in use, e.g. {kinds[0]}"
    return ""


@guarded
def del_vpc(ctx, r):
    """Delete a VPC and its dependents in order. Refuses (failed, with the reason) while anything else still uses it."""
    ec2 = ctx.client("ec2", r.region)
    vpc = r.rid
    why = _vpc_blockers(ec2, ctx.client("elbv2", r.region), vpc)
    if why:
        return Result("failed", f"in use: {why}")
    f = [{"Name": "vpc-id", "Values": [vpc]}]
    for ep in ec2.describe_vpc_endpoints(Filters=f)["VpcEndpoints"]:
        ec2.delete_vpc_endpoints(VpcEndpointIds=[ep["VpcEndpointId"]])
    for ig in ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}])["InternetGateways"]:
        ec2.detach_internet_gateway(InternetGatewayId=ig["InternetGatewayId"], VpcId=vpc)
        ec2.delete_internet_gateway(InternetGatewayId=ig["InternetGatewayId"])
    for sn in ec2.describe_subnets(Filters=f)["Subnets"]:
        ec2.delete_subnet(SubnetId=sn["SubnetId"])
    for rt in ec2.describe_route_tables(Filters=f)["RouteTables"]:
        if any(a.get("Main") for a in rt["Associations"]):
            continue
        for a in rt["Associations"]:
            ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
        ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
    for acl in ec2.describe_network_acls(Filters=f)["NetworkAcls"]:
        if not acl["IsDefault"]:
            ec2.delete_network_acl(NetworkAclId=acl["NetworkAclId"])
    sgs = [g for g in ec2.describe_security_groups(Filters=f)["SecurityGroups"] if g["GroupName"] != "default"]
    for g in sgs:                                   # break cross references between groups first
        if g["IpPermissions"]:
            ec2.revoke_security_group_ingress(GroupId=g["GroupId"], IpPermissions=g["IpPermissions"])
    for g in sgs:
        ec2.delete_security_group(GroupId=g["GroupId"])
    ec2.delete_vpc(VpcId=vpc)
    return Result("deleted")


# (service, rtype) -> (deleter, order). Lower order runs first.
REGISTRY: Dict[Tuple[str, str], Tuple[Callable, int]] = {
    ("cloudfront", "distribution"): (del_distribution, 10),
    ("apigateway", "restapi"): (del_restapi, 20),
    ("apigateway", "httpapi"): (del_httpapi, 20),
    ("lambda", "function"): (del_lambda, 30),
    ("ec2", "instance"): (del_instance, 40),
    ("ec2", "elastic-ip"): (del_eip, 50),
    ("ec2", "volume"): (del_volume, 55),
    ("ec2", "security-group"): (del_sg, 60),
    ("s3", "bucket"): (del_bucket, 70),
    ("dynamodb", "table"): (del_table, 80),
    ("logs", "log-group"): (del_loggroup, 90),
}


# The tag-driven cohort sweep only ever touches these. IAM is owned by Terraform; the rest are
# only deleted through an explicit cleanup ruleset (see rules.py).
TAG_SWEEP_TYPES = list(REGISTRY.keys())


def supported_types():
    return list(TAG_SWEEP_TYPES)


def cleanup_types():
    return list(REGISTRY.keys())


def order_of(r: Resource) -> int:
    return REGISTRY.get(r.key, (None, 999))[1]


# ---- additional types found in real accounts ---------------------------------------------------

@guarded
def del_keypair(ctx, r):
    ctx.client("ec2", r.region).delete_key_pair(KeyName=r.rid)
    return Result("deleted")


@guarded
def del_topic(ctx, r):
    sns = ctx.client("sns", r.region)
    arn = next((t["TopicArn"] for p in sns.get_paginator("list_topics").paginate() for t in p["Topics"]
                if t["TopicArn"].split(":")[-1] == r.rid), None)
    if arn is None:
        return Result("gone")
    sns.delete_topic(TopicArn=arn)
    return Result("deleted")


@guarded
def del_queue(ctx, r):
    sqs = ctx.client("sqs", r.region)
    sqs.delete_queue(QueueUrl=sqs.get_queue_url(QueueName=r.rid)["QueueUrl"])
    return Result("deleted")


@guarded
def del_secret(ctx, r):
    # 7 day recovery window, not an immediate hard delete
    ctx.client("secretsmanager", r.region).delete_secret(SecretId=r.rid, RecoveryWindowInDays=7)
    return Result("deleted")


@guarded
def del_repository(ctx, r):
    ctx.client("ecr", r.region).delete_repository(repositoryName=r.rid, force=True)
    return Result("deleted")


@guarded
def del_stack(ctx, r):
    cfn = ctx.client("cloudformation", r.region)
    try:
        st = cfn.describe_stacks(StackName=r.rid)["Stacks"][0]["StackStatus"]
    except ClientError as e:
        if "does not exist" in e.response["Error"].get("Message", ""):
            return Result("gone")
        raise
    if st == "DELETE_COMPLETE":
        return Result("gone")
    if st == "DELETE_FAILED":
        return Result("failed", "DELETE_FAILED: inspect the stack events")
    if st != "DELETE_IN_PROGRESS":
        cfn.delete_stack(StackName=r.rid)
    return Result("pending", "deleting", 120)


def _tolerant(fn, **kw):
    try:
        fn(**kw)
    except ClientError as e:
        if _code(e) != "NoSuchEntity":
            raise


@guarded
def del_iam_user(ctx, r):
    iam, name = ctx.client("iam"), r.rid
    # 1. Access keys first: deactivate (cuts off use immediately), then delete.
    for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
        if k["Status"] != "Inactive":
            iam.update_access_key(UserName=name, AccessKeyId=k["AccessKeyId"], Status="Inactive")
        iam.delete_access_key(UserName=name, AccessKeyId=k["AccessKeyId"])
    # 2. Same for signing certificates, and MFA devices are deactivated before they are deleted.
    for c in iam.list_signing_certificates(UserName=name)["Certificates"]:
        if c["Status"] != "Inactive":
            iam.update_signing_certificate(UserName=name, CertificateId=c["CertificateId"], Status="Inactive")
        iam.delete_signing_certificate(UserName=name, CertificateId=c["CertificateId"])
    for m in iam.list_mfa_devices(UserName=name)["MFADevices"]:
        iam.deactivate_mfa_device(UserName=name, SerialNumber=m["SerialNumber"])
        _tolerant(iam.delete_virtual_mfa_device, SerialNumber=m["SerialNumber"])
    # 3. Then console access, then everything else that blocks DeleteUser, then the user.
    _tolerant(iam.delete_login_profile, UserName=name)
    for k in iam.list_ssh_public_keys(UserName=name)["SSHPublicKeys"]:
        iam.delete_ssh_public_key(UserName=name, SSHPublicKeyId=k["SSHPublicKeyId"])
    for sc in iam.list_service_specific_credentials(UserName=name)["ServiceSpecificCredentials"]:
        iam.delete_service_specific_credential(UserName=name, ServiceSpecificCredentialId=sc["ServiceSpecificCredentialId"])
    for p in iam.list_user_policies(UserName=name)["PolicyNames"]:
        iam.delete_user_policy(UserName=name, PolicyName=p)
    for p in iam.list_attached_user_policies(UserName=name)["AttachedPolicies"]:
        iam.detach_user_policy(UserName=name, PolicyArn=p["PolicyArn"])
    for g in iam.list_groups_for_user(UserName=name)["Groups"]:
        iam.remove_user_from_group(UserName=name, GroupName=g["GroupName"])
    iam.delete_user(UserName=name)
    return Result("deleted")


@guarded
def del_iam_group(ctx, r):
    iam, name = ctx.client("iam"), r.rid
    for p in iam.list_attached_group_policies(GroupName=name)["AttachedPolicies"]:
        iam.detach_group_policy(GroupName=name, PolicyArn=p["PolicyArn"])
    for p in iam.list_group_policies(GroupName=name)["PolicyNames"]:
        iam.delete_group_policy(GroupName=name, PolicyName=p)
    for u in iam.get_group(GroupName=name)["Users"]:
        iam.remove_user_from_group(GroupName=name, UserName=u["UserName"])
    iam.delete_group(GroupName=name)
    return Result("deleted")


@guarded
def del_iam_role(ctx, r):
    iam, name = ctx.client("iam"), r.rid
    for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
        iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
    for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=p)
    for ip in iam.list_instance_profiles_for_role(RoleName=name)["InstanceProfiles"]:
        iam.remove_role_from_instance_profile(InstanceProfileName=ip["InstanceProfileName"], RoleName=name)
    iam.delete_role(RoleName=name)
    return Result("deleted")


@guarded
def del_iam_policy(ctx, r):
    iam = ctx.client("iam")
    arn = r.attrs.get("arn_override") or r.arn
    if not arn.startswith("arn:"):
        arn = next(p["Arn"] for pg in iam.get_paginator("list_policies").paginate(Scope="Local")
                   for p in pg["Policies"] if p["PolicyName"] == r.rid)
    ents = iam.list_entities_for_policy(PolicyArn=arn)
    for u in ents["PolicyUsers"]:
        iam.detach_user_policy(UserName=u["UserName"], PolicyArn=arn)
    for g in ents["PolicyGroups"]:
        iam.detach_group_policy(GroupName=g["GroupName"], PolicyArn=arn)
    for ro in ents["PolicyRoles"]:
        iam.detach_role_policy(RoleName=ro["RoleName"], PolicyArn=arn)
    for v in iam.list_policy_versions(PolicyArn=arn)["Versions"]:
        if not v["IsDefaultVersion"]:
            iam.delete_policy_version(PolicyArn=arn, VersionId=v["VersionId"])
    iam.delete_policy(PolicyArn=arn)
    return Result("deleted")


REGISTRY.update({
    ("ec2", "vpc"): (del_vpc, 66),
    ("cloudformation", "stack"): (del_stack, 5),
    ("ec2", "key-pair"): (del_keypair, 58),
    ("sns", "topic"): (del_topic, 92),
    ("sqs", "queue"): (del_queue, 93),
    ("secretsmanager", "secret"): (del_secret, 94),
    ("ecr", "repository"): (del_repository, 95),
    ("iam", "user"): (del_iam_user, 100),
    ("iam", "group"): (del_iam_group, 105),
    ("iam", "role"): (del_iam_role, 110),
    ("iam", "policy"): (del_iam_policy, 120),
})


# ---- analytics and observability leftovers ----------------------------------------------------

@guarded
def del_glue_database(ctx, r):
    ctx.client("glue", r.region).delete_database(Name=r.rid)       # catalog metadata only, no S3 data
    return Result("deleted")


@guarded
def del_glue_crawler(ctx, r):
    g = ctx.client("glue", r.region)
    if g.get_crawler(Name=r.rid)["Crawler"]["State"] != "READY":
        return Result("pending", "crawler is running or stopping", 120)
    g.delete_crawler(Name=r.rid)
    return Result("deleted")


@guarded
def del_glue_job(ctx, r):
    ctx.client("glue", r.region).delete_job(JobName=r.rid)
    return Result("deleted")


@guarded
def del_athena_workgroup(ctx, r):
    a = ctx.client("athena", r.region)
    try:
        a.delete_work_group(WorkGroup=r.rid, RecursiveDeleteOption=True)
    except ClientError as e:
        if _code(e) == "InvalidRequestException" and "not found" in e.response["Error"].get("Message", "").lower():
            return Result("gone")
        raise
    return Result("deleted")


@guarded
def del_alarm(ctx, r):
    ctx.client("cloudwatch", r.region).delete_alarms(AlarmNames=[r.rid])
    return Result("deleted")


@guarded
def del_dashboard(ctx, r):
    ctx.client("cloudwatch", r.region).delete_dashboards(DashboardNames=[r.rid])
    return Result("deleted")


@guarded
def del_layer(ctx, r):
    lam = ctx.client("lambda", r.region)
    versions = [v["Version"] for p in lam.get_paginator("list_layer_versions").paginate(LayerName=r.rid)
                for v in p["LayerVersions"]]
    if not versions:
        return Result("gone")
    for v in versions:
        lam.delete_layer_version(LayerName=r.rid, VersionNumber=v)
    return Result("deleted")


REGISTRY.update({
    ("lambda", "layer"): (del_layer, 31),
    ("glue", "crawler"): (del_glue_crawler, 84),
    ("glue", "job"): (del_glue_job, 85),
    ("glue", "database"): (del_glue_database, 86),
    ("athena", "workgroup"): (del_athena_workgroup, 87),
    ("cloudwatch", "alarm"): (del_alarm, 91),
    ("cloudwatch", "dashboard"): (del_dashboard, 91),
})
