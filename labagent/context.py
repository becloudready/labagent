"""Execution context: AWS session, config, account guard, injectable sleep."""
from __future__ import annotations

import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

import boto3

from .config import Config


class GuardError(Exception):
    pass


def log(msg: str) -> None:
    """Logs go to stderr and must never contain names or emails: ids and counts only."""
    print(msg, file=sys.stderr, flush=True)


class Ctx:
    def __init__(self, cfg: Config, session: Optional[boto3.Session] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.session = session or boto3.Session(region_name=cfg.region)
        self.sleep = sleep
        self.clock = clock
        self._clients: Dict[Tuple[str, str], Any] = {}
        self._account: Optional[str] = None

    def client(self, service: str, region: Optional[str] = None):
        key = (service, region or self.cfg.region)
        if key not in self._clients:
            self._clients[key] = self.session.client(service, region_name=key[1])
        return self._clients[key]

    @property
    def account_id(self) -> str:
        if self._account is None:
            self._account = self.client("sts").get_caller_identity()["Account"]
        return self._account

    def guard_account(self) -> None:
        """Refuse to run against any account that is not explicitly allowed."""
        allowed = self.cfg.allowed_account_ids
        if not allowed:
            raise GuardError("allowed_account_ids is empty: refusing to run. Set it in config or LABAGENT_ALLOWED_ACCOUNTS.")
        if self.account_id not in allowed:
            raise GuardError("current AWS account is not in allowed_account_ids: refusing to run.")
