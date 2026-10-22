from labagent.config import Config
from labagent.deleters import supported_types
from labagent.discovery import Resource, Scope, attribute, classify, parse_arn

CFG = Config()
SUP = supported_types()


def res(name="student-ann-fn", tags=None, service="lambda", rtype="function"):
    return Resource(arn=f"arn:{name}", service=service, rtype=rtype, rid=name, region="us-east-1",
                    name=name, tags=tags or {})


def test_parse_arns():
    assert parse_arn("arn:aws:lambda:us-east-1:1:function:fn1").rid == "fn1"
    assert parse_arn("arn:aws:s3:::my-bucket").rtype == "bucket"
    assert parse_arn("arn:aws:dynamodb:us-east-1:1:table/t1").rid == "t1"
    assert parse_arn("arn:aws:ec2:us-east-1:1:elastic-ip/eipalloc-9").rtype == "elastic-ip"
    assert parse_arn("arn:aws:apigateway:us-east-1::/restapis/abc123").rtype == "restapi"
    assert parse_arn("arn:aws:apigateway:us-east-1::/apis/xyz").rtype == "httpapi"
    assert parse_arn("arn:aws:cloudfront::1:distribution/E1").rid == "E1"
    assert parse_arn("arn:aws:logs:us-east-1:1:log-group:/aws/lambda/fn:*").rid == "/aws/lambda/fn"


def test_scope_required():
    import pytest
    with pytest.raises(ValueError):
        Scope().validate()


def test_cohort_match_by_tag_or_date():
    t = {"workshop": "full-stack", "cohort": "c1"}
    assert classify(res(tags=t), CFG, Scope(cohort_id="c1"), SUP)[0] == "delete"
    t2 = {"workshop": "full-stack", "date": "12-Jul-2026"}
    assert classify(res(tags=t2), CFG, Scope(cohort_date="12-Jul-2026"), SUP)[0] == "delete"
    assert classify(res(tags=t2), CFG, Scope(cohort_id="c1"), SUP) == ("skip", "other-cohort")


def test_autodelete_false_protected_case_insensitive():
    t = {"workshop": "full-stack", "cohort": "c1", "AutoDelete": "FALSE"}
    assert classify(res(tags=t), CFG, Scope(cohort_id="c1"), SUP) == ("skip", "autodelete=false")


def test_protected_names():
    t = {"workshop": "full-stack", "cohort": "c1"}
    r = res(name="aws-cloudtrail-logs-xyz", tags=t, service="s3", rtype="bucket")
    assert classify(r, CFG, Scope(cohort_id="c1"), SUP) == ("skip", "protected-name")


def test_unsupported_is_reported_not_deleted():
    r = res(service="iam", rtype="user", tags={"workshop": "full-stack", "cohort": "c1"})
    assert classify(r, CFG, Scope(cohort_id="c1"), SUP)[1] == "unsupported:iam:user"


def test_all_cohorts_explicit():
    t = {"workshop": "full-stack"}
    assert classify(res(tags=t), CFG, Scope(all_cohorts=True), SUP)[0] == "delete"


def test_attribution_boundaries():
    slugs = {"7": "ann-lee", "8": "bo"}
    assert attribute(res(name="student-ann-lee-notice"), CFG, slugs) == "7"
    assert attribute(res(name="annlee-thing"), CFG, slugs) is None
    assert attribute(res(name="bobcat"), CFG, slugs) is None  # "bo" must not match inside a word
    assert attribute(res(name="x", tags={"learner_id": "8"}), CFG, slugs) == "8"
