import pytest

from labagent.plan import PlanError, build_plan, verify_plan

ITEMS = [{"arn": "arn:a", "action": "delete"}, {"arn": "arn:b", "action": "skip"}]


def test_roundtrip_ok():
    p = build_plan("c1", "111", ITEMS, 100.0)
    verify_plan(p, "111", 110.0, 3600, p["hash"])


def test_tampered_items_rejected():
    p = build_plan("c1", "111", ITEMS, 100.0)
    p["items"][1]["action"] = "delete"
    with pytest.raises(PlanError):
        verify_plan(p, "111", 110.0, 3600)


def test_other_account_rejected():
    p = build_plan("c1", "111", ITEMS, 100.0)
    with pytest.raises(PlanError):
        verify_plan(p, "222", 110.0, 3600)


def test_expired_rejected():
    p = build_plan("c1", "111", ITEMS, 100.0)
    with pytest.raises(PlanError):
        verify_plan(p, "111", 100.0 + 7200, 3600)


def test_unapproved_hash_rejected():
    p = build_plan("c1", "111", ITEMS, 100.0)
    with pytest.raises(PlanError):
        verify_plan(p, "111", 110.0, 3600, "deadbeef")
