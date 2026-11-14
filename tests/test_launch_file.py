"""YAML launch files: `labagent launch -f file.yaml` must behave exactly like the same --param flags."""
import json
from pathlib import Path

import pytest

from labagent import cli, spec
from labagent.packs import registry
from labagent.packs.base import Pack, Param, Step
from labagent.spec import SpecError

SEEN = []


class Hello(Pack):
    id, title, summary, default_name = "hello", "Hello", "demo", "dflt"
    params = [Param("who", "world"), Param("hours", "1"), Param("apply", "no"), Param("vms", "")]

    def deploy_steps(self, ctx):
        return [Step("greet", "say hi", lambda c: SEEN.append((c.name, dict(c.params))) or {})]

    def destroy_steps(self, ctx):
        return [Step("bye", "say bye", lambda c: SEEN.append(("destroy:" + c.name, dict(c.params))) or {})]


class Other(Hello):
    id = "other"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(registry, "all_packs", lambda: {"hello": Hello(), "other": Other()})
    SEEN.clear()
    return tmp_path


def write(tmp, text, name="labs.yaml"):
    f = tmp / name
    f.write_text(text, encoding="utf-8")
    return str(f)


# ---- the loader ----

def test_values_become_the_strings_the_command_line_would_give(tmp_path):
    f = write(tmp_path, "pack: hello\nname: demo\nparams:\n  hours: 6\n  who: lab\n  apply: true\n  vms: [a, b, 3]\n  skipped:\n  day: 2026-11-13\n")
    s = spec.load(f)
    assert s == {"pack": "hello", "name": "demo", "params": {"hours": "6", "who": "lab", "apply": "true", "vms": "a,b,3", "day": "2026-11-13"}}


def test_yes_and_no_stay_text_so_apply_yes_means_what_the_flag_means(tmp_path):
    s = spec.load(write(tmp_path, "pack: hello\nparams:\n  a: yes\n  b: no\n  c: on\n  d: off\n  e: false\n  f: 'true'\n"))
    assert s["params"] == {"a": "yes", "b": "no", "c": "on", "d": "off", "e": "false", "f": "true"}


def test_environment_variables_fill_in_and_missing_ones_are_named(tmp_path):
    f = write(tmp_path, "pack: hello\nparams:\n  who: ${KEY_ID}\n  hours: ${HOURS:-4}\n  vms: 'pre-${KEY_ID}-post'\n")
    assert spec.load(f, env={"KEY_ID": "k1"})["params"] == {"who": "k1", "hours": "4", "vms": "pre-k1-post"}
    assert spec.load(f, env={"KEY_ID": "k1", "HOURS": "9"})["params"]["hours"] == "9"
    assert spec.load(f, env={"KEY_ID": "k1", "HOURS": ""})["params"]["hours"] == "4"          # empty counts as unset
    with pytest.raises(SpecError, match=r"params.who: \$\{KEY_ID\} is not set"):
        spec.load(f, env={})


@pytest.mark.parametrize("text,why", [
    ("", "empty"), ("- a\n- b\n", "must be a mapping"), ("pack: hello\nparam:\n  a: 1\n", "unknown key.*param.*Allowed"),
    ("pack: hello\nparams: [a, b]\n", "params must be a mapping"), ("pack: hello\nparams:\n  a: {b: 1}\n", "plain value"),
    ("pack: hello\nparams:\n  a: [[1], 2]\n", "plain value"), ("pack: 'hello world'\n", "pack must be"), ("pack: hello\n  bad: indent\n: x\n", "invalid YAML at line"),
    ("pack: !!python/object/apply:os.system ['echo hacked']\n", "invalid YAML"),
])
def test_unusable_files_say_what_to_change(tmp_path, text, why):
    with pytest.raises(SpecError, match=why):
        spec.load(write(tmp_path, text))


def test_missing_and_huge_files(tmp_path):
    with pytest.raises(SpecError, match="cannot read"):
        spec.load(str(tmp_path / "nope.yaml"))
    with pytest.raises(SpecError, match="not a launch file"):
        spec.load(write(tmp_path, "pack: hello\n# " + "x" * 1_100_000))


# ---- the command line ----

def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap


def test_launch_uses_pack_name_and_params_from_the_file(home, capsys):
    f = write(home, "pack: hello\nname: spring-workshop\nparams:\n  who: students\n  hours: 6\n")
    code, cap = run_cli(capsys, "launch", "-f", f)
    assert code == 0 and json.loads(cap.out)["status"] == "ok"
    assert SEEN == [("spring-workshop", {"who": "students", "hours": "6", "apply": "no", "vms": ""})]


def test_flags_win_over_the_file(home, capsys):
    f = write(home, "pack: hello\nname: from-file\nparams:\n  who: file\n  hours: 6\n")
    run_cli(capsys, "launch", "-f", f, "--name", "from-flag", "--param", "hours=2")
    name, params = SEEN[0]
    assert name == "from-flag" and params["hours"] == "2" and params["who"] == "file"


def test_destroy_takes_the_same_file(home, capsys):
    f = write(home, "pack: hello\nname: spring-workshop\nparams:\n  who: students\n")
    code, _ = run_cli(capsys, "destroy", "-f", f)
    assert code == 0 and SEEN[0][0] == "destroy:spring-workshop" and SEEN[0][1]["who"] == "students"


def test_dry_run_with_a_file_runs_nothing(home, capsys):
    f = write(home, "pack: hello\nname: x\n")
    code, cap = run_cli(capsys, "launch", "-f", f, "--dry-run")
    assert code == 0 and SEEN == [] and json.loads(cap.out)["mode"] == "dry-run"


def test_a_typo_in_a_param_name_is_caught_like_on_the_command_line(home, capsys):
    f = write(home, "pack: hello\nparams:\n  hourz: 6\n")
    code, cap = run_cli(capsys, "launch", "-f", f)
    assert code == 2 and "unknown parameter(s) for hello: hourz" in json.loads(cap.out)["error"] and SEEN == []


def test_pack_on_the_command_line_must_match_the_file(home, capsys):
    f = write(home, "pack: other\n")
    code, cap = run_cli(capsys, "launch", "hello", "-f", f)
    assert code == 2 and "is for the pack 'other', not 'hello'" in cap.err and SEEN == []
    f = write(home, "pack: hello\n", "ok.yaml")
    assert run_cli(capsys, "launch", "hello", "-f", f)[0] == 0


def test_no_pack_and_no_file_and_bad_files_are_exit_2_with_a_message(home, capsys):
    code, cap = run_cli(capsys, "launch")
    assert code == 2 and "give a pack name, or a launch file" in cap.err
    code, cap = run_cli(capsys, "launch", "-f", write(home, "param: 1\n"))
    assert code == 2 and "unknown key(s) param" in cap.err and cap.out == ""
    code, cap = run_cli(capsys, "launch", "-f", write(home, "who: x\n", "nopack.yaml"))
    assert code == 2


def test_the_plain_command_line_still_works(home, capsys):
    assert run_cli(capsys, "launch", "hello", "--name", "x", "--param", "who=cli")[0] == 0 and SEEN[0][1]["who"] == "cli"


# ---- a typical gpu-notebooks file ----

TYPICAL = """
description: JupyterLab per H100 slice for a two-hour workshop
pack: gpu-notebooks
name: nvidia-h100-workshop
params:
  cloud: shadeform
  vendor: scaleway
  region: paris-france-1
  os: ubuntu24.04_cuda13.0_shade_os
  ssh_key_id: ${SHADEFORM_SSH_KEY_ID}
  hours: 4
  mode: mig
  profile: auto
  login_user: labagent
  login_sudo: false
"""


def test_a_typical_file_only_uses_parameters_the_real_pack_has(monkeypatch, tmp_path):
    monkeypatch.setenv("SHADEFORM_SSH_KEY_ID", "key-1")
    from labagent.packs.gpu_notebooks.pack import GpuNotebooks
    f = tmp_path / "workshop.yaml"
    f.write_text(TYPICAL, encoding="utf-8")
    s = spec.load(str(f))
    assert s["pack"] == "gpu-notebooks" and s["name"] == "nvidia-h100-workshop"
    assert s["params"]["ssh_key_id"] == "key-1"
    assert set(s["params"]) <= {p.name for p in GpuNotebooks().params}
    assert GpuNotebooks().resolve(s["params"])["login_sudo"] == "false"
