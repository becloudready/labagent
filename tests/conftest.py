import os

import boto3
import pytest
from moto import mock_aws

from labagent.config import Config
from labagent.context import Ctx

ACCOUNT = "123456789012"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": "us-east-1", "MOTO_ACCOUNT_ID": ACCOUNT,
                 "MOTO_IAM_LOAD_MANAGED_POLICIES": "true"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def cfg():
    return Config(allowed_account_ids=[ACCOUNT], workshop="full-stack")


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


class IamShim:
    """moto does not implement list_service_specific_credentials (real IAM does); answer 'none'."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def list_service_specific_credentials(self, **kw):
        return {"ServiceSpecificCredentials": []}


@pytest.fixture
def ctx(aws, cfg, clock):
    c = Ctx(cfg, boto3.Session(region_name="us-east-1"), sleep=clock.sleep, clock=clock.now)
    c._clients[("iam", cfg.region)] = IamShim(c.session.client("iam", region_name=cfg.region))
    return c


TAGS = [{"Key": "workshop", "Value": "full-stack"}, {"Key": "autodelete", "Value": "true"},
        {"Key": "date", "Value": "12-Jul-2026"}]


def tagd(**extra):
    return {t["Key"]: t["Value"] for t in TAGS} | extra if False else {**{t["Key"]: t["Value"] for t in TAGS}, **extra}
