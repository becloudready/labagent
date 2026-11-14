"""Job, config and ruleset files may be YAML. A YAML file must mean exactly what the same JSON would, and nothing more."""
import json
from pathlib import Path

import pytest

from labagent import cli, config
from labagent.packs import registry
from labagent.spec import SpecError, load_document

ROOT = Path(__file__).resolve().parents[1]


def write(tmp, name, text):
    f = tmp / name
    f.write_text(text, encoding="utf-8")
    return str(f)


# ---- reading ----

def test_the_file_type_comes_from_the_name(tmp_path):
    body = {"a": 1, "b": ["x"], "c": {"d": True}}
    assert load_document(write(tmp_path, "x.json", json.dumps(body))) == body
    assert load_document(write(tmp_path, "x", json.dumps(body))) == body                          # no extension: JSON, as before
    assert load_document(write(tmp_path, "x.YAML", "a: 1\nb: [x]\nc: {d: true}\n")) == body
    assert load_document(write(tmp_path, "x.yml", "a: 1\nb: [x]\nc: {d: true}\n")) == body


def test_values_keep_the_type_the_same_json_would_give(tmp_path):
    doc = load_document(write(tmp_path, "d.yaml", "n: 12\nf: 1.5\nt: true\nf2: False\nz: null\ne:\ns: apply\nneg: -3\nmax: 21600\n"))
    assert doc == {"n": 12, "f": 1.5, "t": True, "f2": False, "z": None, "e": None, "s": "apply", "neg": -3, "max": 21600}
    assert type(doc["n"]) is int and type(doc["t"]) is bool


@pytest.mark.parametrize("text", ["007", "0x1F", "1_000", "1:30", "0o17", "000000000000", "2026-11-13", "2026-11-13 10:00:00", "1e3", "yes", "no", "on", "off", "y", "n"])
def test_nothing_is_quietly_reinterpreted(tmp_path, text):
    """YAML 1.1 reads 007 as octal, 1:30 as 90 and yes as true. An id or a date must reach labagent as the text that was written."""
    assert load_document(write(tmp_path, "d.yaml", f"v: {text}\n")) == {"v": text}


def test_a_quoted_number_is_text_and_an_unquoted_one_is_a_number(tmp_path):
    assert load_document(write(tmp_path, "d.yaml", 'a: "007"\nb: 7\nc: "7"\n')) == {"a": "007", "b": 7, "c": "7"}


def test_a_repeated_key_is_an_error_not_a_silent_winner(tmp_path):
    with pytest.raises(SpecError, match="appears twice"):
        load_document(write(tmp_path, "d.yaml", "region: us-east-1\nworkshop: a\nregion: eu-west-1\n"))
    with pytest.raises(SpecError, match="appears twice"):
        load_document(write(tmp_path, "d.yaml", "rules:\n  - id: a\n    id: b\n"))


def test_aliases_are_refused(tmp_path):
    with pytest.raises(SpecError, match="aliases"):
        load_document(write(tmp_path, "d.yaml", "a: &x [1, 2]\nb: *x\n"))
    load_document(write(tmp_path, "ok.yaml", "a: &x [1, 2]\n"))                                   # an anchor nobody uses is harmless


def test_yaml_cannot_build_python_objects(tmp_path):
    with pytest.raises(SpecError):
        load_document(write(tmp_path, "d.yaml", "a: !!python/object/apply:os.system ['echo nope']\n"))


@pytest.mark.parametrize("name,text,why", [
    ("empty.yaml", "", "empty"),
    ("list.yaml", "- a\n- b\n", "list"),
    ("scalar.yml", "hello\n", "str"),
    ("list.json", "[1]", "list"),
    ("null.json", "null", "empty"),
])
def test_the_top_level_must_be_a_mapping(tmp_path, name, text, why):
    with pytest.raises(SpecError, match=f"top level must be a mapping.*{why}"):
        load_document(write(tmp_path, name, text))


def test_a_broken_file_names_the_file_and_the_line(tmp_path):
    with pytest.raises(SpecError, match=r"bad\.yaml: invalid YAML at line 2"):
        load_document(write(tmp_path, "bad.yaml", "a: 1\n\tb: 2\n"))
    with pytest.raises(SpecError, match=r"bad\.json: invalid JSON"):
        load_document(write(tmp_path, "bad.json", "{not json"))


def test_missing_huge_and_binary_files(tmp_path):
    with pytest.raises(SpecError, match="cannot read the file"):
        load_document(str(tmp_path / "nope.yaml"))
    with pytest.raises(SpecError, match="larger than"):
        load_document(write(tmp_path, "big.yaml", "a: " + "x" * 1_100_000 + "\n"))
    (tmp_path / "bin.yaml").write_bytes(b"a: \xff\xfe\n")
    with pytest.raises(SpecError, match="not UTF-8"):
        load_document(str(tmp_path / "bin.yaml"))


def test_long_multiline_values_such_as_a_pem_survive(tmp_path):
    doc = load_document(write(tmp_path, "j.yaml", "pem: |\n  -----BEGIN PUBLIC KEY-----\n  AAAA\n  -----END PUBLIC KEY-----\n"))
    assert doc["pem"] == "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"


# ---- config ----

def test_a_yaml_config_loads_and_means_what_the_json_does(tmp_path):
    y = write(tmp_path, "lab.yaml", 'region: eu-west-1\nallowed_account_ids: ["000000000000"]\nbackend: {bucket: b, use_lockfile: false}\nplan_max_age_s: 60\n')
    j = write(tmp_path, "lab.json", json.dumps({"region": "eu-west-1", "allowed_account_ids": ["000000000000"], "backend": {"bucket": "b", "use_lockfile": False},
                                                 "plan_max_age_s": 60}))
    assert config.load_config(y) == config.load_config(j)
    assert config.load_config(y).allowed_account_ids == ["000000000000"]


def test_the_config_path_may_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_CONFIG", write(tmp_path, "lab.yml", "workshop: from-env\n"))
    assert config.load_config().workshop == "from-env"


def test_an_unquoted_account_id_is_refused_and_a_leading_zero_survives(tmp_path):
    assert config.load_config(write(tmp_path, "ok.yaml", "allowed_account_ids: [000000000000]\n")).allowed_account_ids == ["000000000000"]
    with pytest.raises(ValueError, match="quoted strings"):
        config.load_config(write(tmp_path, "lab.yaml", "allowed_account_ids: [123456789012]\n"))
    with pytest.raises(ValueError, match="quoted strings"):
        config.load_config(write(tmp_path, "lab.json", '{"allowed_account_ids": [123456789012]}'))
    with pytest.raises(ValueError, match="quoted strings"):
        config.load_config(write(tmp_path, "lab2.yaml", 'allowed_account_ids: "123456789012"\n'))          # a string, not a list


def test_an_unknown_config_key_is_still_an_error(tmp_path):
    with pytest.raises(ValueError, match="unknown config keys"):
        config.load_config(write(tmp_path, "lab.yaml", "regoin: us-east-1\n"))


# ---- the examples shipped with the repo ----

def test_the_example_config_loads():
    cfg = config.load_config(str(ROOT / "examples" / "config.example.yaml"))
    assert cfg.allowed_account_ids == ["123456789012"] and cfg.backend["use_lockfile"] is True and cfg.plan_max_age_s == 21600


def test_the_example_job_has_the_fields_the_contract_needs():
    job = load_document(str(ROOT / "examples" / "job.provision.example.yaml"))
    assert job["command"] == "provision" and job["mode"] == "apply" and job["cohort"] == {"id": "demo-cohort-1", "created_date": "01-Jan-2030"}
    assert [lr["learner_id"] for lr in job["learners"]] == ["1", "2"]
    assert job["secrets_pubkey_pem"].startswith("-----BEGIN PUBLIC KEY-----\n") and job["secrets_pubkey_pem"].endswith("-----END PUBLIC KEY-----\n")


def test_the_example_ruleset_is_a_valid_ruleset():
    from labagent import rules
    rs = load_document(str(ROOT / "examples" / "rulesets" / "legacy-cleanup.example.yaml"))
    assert rs["version"] == 1 and len(rs["rules"]) == 5
    assert [r["id"] for r in rs["rules"] if r.get("enabled") is False] == ["stale-key-pairs"]
    rules.validate(rs)                                                                             # the engine accepts it as written


# ---- the CLI ----

def test_run_takes_a_yaml_job_for_a_pack(tmp_path, monkeypatch, capsys):
    from tests.test_pack_jobs import Fake
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(registry, "all_packs", lambda: {"fake-ide": Fake()})
    job = write(tmp_path, "job.yaml", "run_id: r1\ncommand: launch\npack: fake-ide\nname: demo\nparams:\n  fail: false\n")
    out = tmp_path / "result.json"
    assert cli.main(["run", "--job", job, "--output", str(out)]) == 0
    assert json.loads(out.read_text())["status"] == "ok"


def test_run_merges_a_yaml_ruleset_into_the_job_and_reads_a_yaml_config(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.boto3, "Session", lambda **kw: object())
    monkeypatch.setattr(cli, "Ctx", lambda cfg, session: seen.update(cfg=cfg) or object())

    def fake_run(ctx, job):
        seen["job"] = job
        return cli.Envelope(command=job["command"])
    monkeypatch.setattr(cli, "run_job", fake_run)
    conf = write(tmp_path, "lab.yaml", 'region: eu-west-1\nallowed_account_ids: ["123456789012"]\n')
    job = write(tmp_path, "job.yaml", "run_id: r1\ncommand: audit\n")
    rules = write(tmp_path, "rules.yaml", "version: 1\nname: t\nrules:\n  - id: k\n    action: keep\n    match: {type: [iam/user]}\n")
    assert cli.main(["--config", conf, "run", "--job", job, "--ruleset", rules, "--output", str(tmp_path / "r.json")]) == 0
    assert seen["cfg"].region == "eu-west-1"
    assert seen["job"]["ruleset"]["rules"][0]["id"] == "k"


def test_a_job_file_that_cannot_be_read_gives_a_result_not_a_traceback(tmp_path):
    out = tmp_path / "result.json"
    assert cli.main(["run", "--job", write(tmp_path, "job.yaml", "a: 1\na: 2\n"), "--output", str(out)]) == 2
    res = json.loads(out.read_text())
    assert res["status"] == "failed" and "appears twice" in res["error"] and "job.yaml" in res["error"]
