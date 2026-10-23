"""EC2 platform for rag-ide: ONE shared instance per cohort, one container per learner.

Why not one VM per learner: learners are idle most of the time, so they share CPU, and a single box with a pinned image costs
a small fraction of per-learner serverless sandboxes. What keeps it safe:
  - no SSH and no instance profile (learners get no AWS credentials); only ports 80 and 443 are open
  - IMDSv2 required with hop limit 1, so containers cannot reach the instance metadata service
  - every learner container is unprivileged, memory/CPU/pid limited, with all Linux capabilities dropped
  - the box shuts itself down (and terminates) after `hours`, and also when no learner container is left running
  - the account must be on the labagent allow list; the image source is a pinned git commit that is verified after checkout
HTTPS comes from Caddy and Let's Encrypt on a <ip>.sslip.io name, so no domain is needed. Learner URLs are paths on that name.
"""
from __future__ import annotations

import math
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from ..base import PackError
from .modal_gateway import slug

REPO = "https://github.com/becloudready/labagent"
PINNED_REF = "737a284413ae94bb69cd425935df0f31e3919a28"    
CANONICAL_OWNER = "099720109477"
AMI_NAME = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
SSM_UBUNTU = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
IMAGE = "labagent-rag-lab:local"
# (type, vCPU, GiB) smallest first. x86_64 only (the image is built on the box).
INSTANCE_TYPES: List[Tuple[str, int, int]] = [
    ("m6i.large", 2, 8), ("m6i.xlarge", 4, 16), ("m6i.2xlarge", 8, 32), ("m6i.4xlarge", 16, 64), ("m6i.8xlarge", 32, 128),
]
OVERSUBSCRIBE_RAM = 0.5      # learners rarely use their whole memory limit at the same time
LEARNERS_PER_VCPU = 3        # CPU is shared more aggressively than memory


def pick_instance_type(learners: int, memory_mib: int, override: str = "auto") -> str:
    """Smallest type with RAM >= 50% of the sum of learner limits (plus 2 GiB system) and ~3 learners per vCPU."""
    if override != "auto":
        if not re.fullmatch(r"[a-z0-9]+\.[a-z0-9]+", override):
            raise PackError("instance_type must look like m6i.2xlarge, or be 'auto'")
        return override
    need_gib = learners * (memory_mib / 1024.0) * OVERSUBSCRIBE_RAM + 2
    need_cpu = math.ceil(learners / LEARNERS_PER_VCPU)
    for t, cpu, gib in INSTANCE_TYPES:
        if gib >= need_gib and cpu >= need_cpu:
            return t
    raise PackError("that many learners do not fit one instance: split them into two cohorts")


def sslip_host(ip: str) -> str:
    return ip.replace(".", "-") + ".sslip.io"


def learner_url(host: str, learner: str) -> str:
    return f"https://{host}/{learner}/"


def build_user_data(learners: Dict[str, str], *, ref: str, hours: int, idle_minutes: int, cpu: float, memory_mib: int,
                    tls: str = "letsencrypt", env: Optional[Dict[str, str]] = None) -> str:
    """cloud-init script. `learners` maps name -> password. Passwords are in user-data, which anyone with EC2 read access in the
    account can see: this is why the account guard matters. Learner containers cannot read it (hop limit 1)."""
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise PackError("ref must be a full 40-character commit SHA")
    for n, pw in learners.items():
        if not re.fullmatch(r"[a-z0-9-]+", n) or not re.fullmatch(r"[A-Za-z0-9_-]{8,}", pw):
            raise PackError("learner names and passwords must be plain [a-z0-9-] / [A-Za-z0-9_-] tokens")
    q = shlex.quote
    extra = " ".join(f"-e {q(k)}={q(v)}" for k, v in (env or {}).items())
    runs, routes = [], []
    for n, pw in learners.items():
        runs.append(f"""
docker volume create lab-{n} >/dev/null
docker run --rm -u root -v lab-{n}:/w {IMAGE} chown -R 1000:1000 /w
docker run -d --name lab-{n} --network labnet --cpus {cpu:g} --memory {memory_mib}m --pids-limit 1024 \\
  --cap-drop ALL --security-opt no-new-privileges -e PASSWORD={q(pw)} -e HF_HUB_OFFLINE=1 {extra} \\
  -v lab-{n}:/home/coder/lab/work {IMAGE} \\
  code-server --bind-addr 0.0.0.0:8080 --auth password --idle-timeout-seconds {idle_minutes * 60} /home/coder/lab""")
        routes.append(f"  redir /{n} /{n}/\n  handle_path /{n}/* {{\n    reverse_proxy lab-{n}:8080\n  }}")
    tls_line = "  tls internal\n" if tls == "internal" else ""
    return f"""#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/labagent-boot.log) 2>&1
shutdown -h +{hours * 60} "labagent: hard time cap reached"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y && apt-get install -y docker.io git curl
systemctl enable --now docker
TOKEN=$(curl -sS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
IP=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
HOST=$(echo "$IP" | tr . -).sslip.io
mkdir -p /opt/labagent/src && cd /opt/labagent/src
git init -q && git remote add origin {REPO} && git fetch -q --depth 1 origin {ref} && git checkout -q FETCH_HEAD
test "$(git rev-parse HEAD)" = "{ref}"
docker build -q -f Dockerfile.lab -t {IMAGE} .
docker network create labnet
{''.join(runs)}
mkdir -p /opt/labagent/caddy
cat > /opt/labagent/caddy/Caddyfile <<CADDY
$HOST {{
{tls_line}{chr(10).join(routes)}
}}
CADDY
docker run -d --name caddy --restart unless-stopped --network labnet -p 80:80 -p 443:443 \\
  -v /opt/labagent/caddy/Caddyfile:/etc/caddy/Caddyfile:ro -v caddy_data:/data caddy:2
cat > /usr/local/bin/labagent-idle-check <<'IDLE'
#!/bin/bash
# Stop paying when every learner has gone: shut down after {max(5, idle_minutes)} minutes with no running learner container.
n=$(docker ps --filter name=lab- --format '{{{{.Names}}}}' | wc -l)
f=/run/labagent-empty-since
if [ "$n" -gt 0 ]; then rm -f "$f"; exit 0; fi
[ -f "$f" ] || date +%s > "$f"
[ $(( $(date +%s) - $(cat "$f") )) -ge {max(5, idle_minutes) * 60} ] && shutdown -h now
IDLE
chmod +x /usr/local/bin/labagent-idle-check
echo '* * * * * root /usr/local/bin/labagent-idle-check' > /etc/cron.d/labagent-idle
echo READY > /var/log/labagent-ready
"""


class Ec2Gateway:
    """Real AWS calls, guarded by the allow list. Imported lazily by the pack so tests can substitute a fake."""

    def __init__(self, region: Optional[str] = None, profile: Optional[str] = None, session: Any = None, config: Any = None):
        from ...config import load_config
        from ...context import Ctx
        import boto3
        self.cfg = config or load_config()
        if region:
            self.cfg.region = region
        self.ctx = Ctx(self.cfg, session or boto3.Session(profile_name=profile, region_name=self.cfg.region))

    def check_auth(self) -> str:
        from ...context import GuardError
        try:
            self.ctx.guard_account()
        except GuardError as e:
            raise PackError(f"{e} Example: LABAGENT_ALLOWED_ACCOUNTS=<your account id>") from e
        except Exception as e:
            raise PackError(f"could not read AWS credentials ({type(e).__name__}). Configure them or pass --profile") from e
        return f"{self.ctx.account_id} in {self.cfg.region}"

    @property
    def ec2(self):
        return self.ctx.client("ec2")

    def ami(self) -> str:
        """Latest Ubuntu 24.04 from Canonical. SSM is the clean source; some roles cannot read it, so fall back to DescribeImages."""
        try:
            return self.ctx.client("ssm").get_parameter(Name=SSM_UBUNTU)["Parameter"]["Value"]
        except Exception:
            imgs = self.ec2.describe_images(Owners=[CANONICAL_OWNER], Filters=[{"Name": "name", "Values": [AMI_NAME]}, {"Name": "state", "Values": ["available"]}])["Images"]
            if not imgs:
                raise PackError("could not find an Ubuntu 24.04 image: allow ssm:GetParameter or ec2:DescribeImages for this identity")
            return max(imgs, key=lambda i: i["CreationDate"])["ImageId"]

    def security_group(self, cohort: str) -> str:
        name = f"labagent-rag-ide-{slug(cohort, 30)}"
        found = self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}])["SecurityGroups"]
        if found:
            return found[0]["GroupId"]
        vpc = self.ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpc:
            raise PackError("no default VPC in this region: create one or use another region")
        sg = self.ec2.create_security_group(GroupName=name, Description="labagent rag-ide: HTTP/HTTPS only", VpcId=vpc[0]["VpcId"],
                                            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "labagent", "Value": "rag-ide"}, {"Key": "cohort", "Value": slug(cohort)}]}])["GroupId"]
        self.ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": p, "ToPort": p, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "labagent learners"}]} for p in (80, 443)])
        return sg

    def run(self, cohort: str, sg: str, user_data: str, instance_type: str, spot: bool) -> str:
        kwargs: Dict[str, Any] = dict(
            ImageId=self.ami(), InstanceType=instance_type, MinCount=1, MaxCount=1, UserData=user_data, SecurityGroupIds=[sg],
            InstanceInitiatedShutdownBehavior="terminate",
            MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"},
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 60, "VolumeType": "gp3", "DeleteOnTermination": True, "Encrypted": True}}],
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "labagent", "Value": "rag-ide"}, {"Key": "cohort", "Value": slug(cohort)},
                                                                    {"Key": "Name", "Value": f"labagent-rag-ide-{slug(cohort)}"}]}])
        if spot:
            kwargs["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}}
        return self.ec2.run_instances(**kwargs)["Instances"][0]["InstanceId"]

    def public_ip(self, instance_id: str, timeout_s: int = 300) -> str:
        waited = 0
        while waited <= timeout_s:
            r = self.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
            if r["State"]["Name"] == "running" and r.get("PublicIpAddress"):
                return r["PublicIpAddress"]
            if r["State"]["Name"] in ("shutting-down", "terminated"):
                raise PackError("the instance terminated during startup (spot capacity or a quota?)")
            self.ctx.sleep(5)
            waited += 5
        raise PackError("the instance did not get a public IP in time")

    def pause(self, seconds: float) -> None:
        self.ctx.sleep(seconds)

    def ready(self, host: str, learner: str) -> bool:
        """True once Caddy serves the learner's login page over valid HTTPS."""
        import urllib.request
        try:
            with urllib.request.urlopen(learner_url(host, learner) + "login", timeout=10) as r:
                return r.status == 200
        except Exception:
            return False

    def alive(self, instance_id: str) -> bool:
        try:
            r = self.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
            return r["State"]["Name"] in ("pending", "running")
        except Exception:
            return False

    def find(self, cohort: str) -> List[str]:
        res = self.ec2.describe_instances(Filters=[{"Name": "tag:labagent", "Values": ["rag-ide"]}, {"Name": "tag:cohort", "Values": [slug(cohort)]},
                                                    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        return [i["InstanceId"] for r in res["Reservations"] for i in r["Instances"]]

    def terminate(self, ids: List[str]) -> None:
        if ids:
            self.ec2.terminate_instances(InstanceIds=ids)
            self.ec2.get_waiter("instance_terminated").wait(InstanceIds=ids, WaiterConfig={"Delay": 5, "MaxAttempts": 60})

    def delete_security_group(self, cohort: str) -> None:
        name = f"labagent-rag-ide-{slug(cohort, 30)}"
        for g in self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "tag:labagent", "Values": ["rag-ide"]}])["SecurityGroups"]:
            try:
                self.ec2.delete_security_group(GroupId=g["GroupId"])
            except Exception:
                pass        # still attached for a few seconds after termination; the next destroy removes it
