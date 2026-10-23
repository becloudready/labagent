import csv
import os

import pytest

from labagent.packs import registry
from labagent.packs.base import load_state, run_pack
from labagent.packs.rag_ide import modal_gateway as mg
from labagent.packs.rag_ide.pack import RagIde, creds_path, learner_names


class FakeModal:
    def __init__(self, logged_in=True):
        self.logged_in = logged_in
        self.sandboxes = {}
        self.volumes = set()
        self.launches = []
        self.builds = 0
        self.deleted = []

    def check_auth(self):
        if not self.logged_in:
            from labagent.packs.base import PackError
            raise PackError("no Modal login found. Run: modal token new")
        return "acme-workspace"

    def app(self, name): return {"app": name}
    def image(self, source): return {"image": source}
    def build(self, app, image): self.builds += 1

    def launch(self, app, cohort, learner, password, image, cpu, memory_mib, timeout_s, idle_s, env):
        sid = f"sb-{len(self.launches) + 1}"
        self.launches.append({"learner": learner, "password": password, "timeout_s": timeout_s, "idle_s": idle_s, "env": env, "cpu": cpu})
        self.sandboxes[sid] = True
        vol = mg.volume_name(cohort, learner)
        self.volumes.add(vol)
        return {"sandbox_id": sid, "url": f"https://{learner}.example.modal.host", "volume": vol}

    def is_alive(self, sid): return self.sandboxes.get(sid, False)
    def stop(self, sid): self.sandboxes[sid] = False
    def find(self, cohort): return [s for s, up in self.sandboxes.items() if up]
    def delete_volume(self, name): self.deleted.append(name); self.volumes.discard(name)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))


def launch(fake, **params):
    return run_pack(RagIde(), "launch", "c1", {"learners": "2", "image": "pypi", **params}, services={"modal": fake})


def test_registered_and_resolvable_by_alias():
    assert registry.resolve("rag-ide").id == "rag-ide" and registry.resolve("web-ide").id == "rag-ide"


def test_learner_names_count_list_and_limits():
    assert learner_names("3") == ["learner-01", "learner-02", "learner-03"]
    assert learner_names("Ana, Ben Q") == ["ana", "ben-q"]
    for bad in ("", "a,A", "99"):
        with pytest.raises(Exception):
            learner_names(bad)


def test_launch_starts_each_learner_with_caps_and_writes_private_credentials(tmp_path):
    fake = FakeModal()
    env = launch(fake, hours=3, idle_minutes=10, cpu=1.5)
    assert env.exit_code == 0 and len(fake.launches) == 2 and fake.builds == 1
    assert fake.launches[0]["timeout_s"] == 3 * 3600 and fake.launches[0]["idle_s"] == 600 and fake.launches[0]["cpu"] == 1.5
    assert fake.launches[0]["env"] == {}                         # no LLM key unless asked
    path = creds_path("c1")
    assert path.stat().st_mode & 0o777 == 0o600
    rows = list(csv.DictReader(path.open()))
    assert [r["learner"] for r in rows] == ["learner-01", "learner-02"] and all(len(r["password"]) >= 12 for r in rows)
    assert len({r["password"] for r in rows}) == 2
    # passwords never reach persisted state or the envelope
    blob = str(load_state("rag-ide", "c1")) + str(env.__dict__)
    assert not any(r["password"] in blob for r in rows)


def test_rerun_resumes_and_only_restarts_dead_learners():
    fake = FakeModal()
    launch(fake)
    first = {r["learner"]: r["password"] for r in csv.DictReader(creds_path("c1").open())}
    fake.sandboxes["sb-1"] = False                               # learner-01 timed out
    launch(fake)
    assert len(fake.launches) == 3 and fake.launches[2]["learner"] == "learner-01"
    after = {r["learner"]: r["password"] for r in csv.DictReader(creds_path("c1").open())}
    assert after["learner-02"] == first["learner-02"] and after["learner-01"] != first["learner-01"]


def test_groq_shared_requires_a_key_and_passes_it_only_when_asked(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    env = launch(FakeModal(), llm="groq-shared")
    assert env.exit_code != 0 and "GROQ_API_KEY" in str(env.__dict__)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    fake = FakeModal()
    assert launch(fake, llm="groq-shared").exit_code == 0
    assert fake.launches[0]["env"]["GROQ_API_KEY"] == "gsk_test" and fake.launches[0]["env"]["LLM_PROVIDER"] == "groq"
    assert "gsk_test" not in str(load_state("rag-ide", "c1")) and "gsk_test" not in creds_path("c1").read_text()


def test_not_logged_in_fails_with_the_fix():
    env = launch(FakeModal(logged_in=False))
    assert env.exit_code != 0 and "modal token new" in str(env.__dict__)


def test_bad_inputs_are_rejected_before_anything_runs():
    fake = FakeModal()
    for bad in ({"hours": 0}, {"hours": 25}, {"llm": "other"}, {"learners": "x,x"}):
        assert launch(fake, **bad).exit_code != 0
    assert fake.launches == [] and fake.builds == 0


def test_destroy_stops_everyone_deletes_volumes_and_credentials():
    fake = FakeModal()
    launch(fake)
    env = run_pack(RagIde(), "destroy", "c1", {}, services={"modal": fake})
    assert env.exit_code == 0 and not any(fake.sandboxes.values())
    assert sorted(fake.deleted) == sorted([mg.volume_name("c1", "learner-01"), mg.volume_name("c1", "learner-02")]) and not creds_path("c1").exists()


def test_destroy_keep_work_keeps_volumes():
    fake = FakeModal()
    launch(fake)
    run_pack(RagIde(), "destroy", "c1", {"keep_work": True}, services={"modal": fake})
    assert fake.deleted == [] and not any(fake.sandboxes.values())


def test_destroy_also_stops_orphans_found_by_tag():
    fake = FakeModal()
    fake.sandboxes["orphan"] = True                              # started earlier, state file lost
    run_pack(RagIde(), "destroy", "c1", {}, services={"modal": fake})
    assert fake.sandboxes["orphan"] is False


def test_start_script_drops_root_and_sets_idle_shutdown():
    s = mg.start_script(900)
    assert "runuser -u coder" in s and "--idle-timeout-seconds 900" in s and "--auth password" in s and s.index("chown") < s.index("exec") and "readlink -f" in s


def test_volume_and_app_names_are_safe():
    assert mg.volume_name("My Cohort!", "Ana B.") == "labagent-rag-ide-my-cohort-ana-b"
    assert len(mg.volume_name("x" * 90, "y" * 90)) <= 64


def test_real_modal_config_is_read_correctly(monkeypatch):
    pytest.importorskip("modal")
    import modal.config
    from labagent.packs.base import PackError
    gw = mg.ModalGateway()
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-test")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-test")
    assert gw.check_auth()                                   # real config.get("token_id") path, the one that once raised KeyError
    monkeypatch.setattr(modal.config.config, "get", lambda key, *a, **k: None)
    with pytest.raises(PackError, match="modal token new"):
        gw.check_auth()


def run_logged(fake, **params):
    lines = []
    env = run_pack(RagIde(), "launch", "c1", {"image": "pypi", **params}, services={"modal": fake}, log=lines.append)
    return env, "\n".join(lines)


def test_urls_are_printed_and_small_cohorts_also_see_passwords():
    fake = FakeModal()
    env, out = run_logged(fake, learners="2")
    rows = list(csv.DictReader(creds_path("c1").open()))
    assert env.exit_code == 0 and all(r["url"] in out for r in rows) and all(r["password"] in out for r in rows)


def test_large_cohorts_print_urls_but_not_passwords_unless_asked():
    env, out = run_logged(FakeModal(), learners="5")
    rows = list(csv.DictReader(creds_path("c1").open()))
    assert all(r["url"] in out for r in rows) and not any(r["password"] in out for r in rows) and "cat " in out
    _, shown = run_logged(FakeModal(), learners="5", show_passwords="yes")
    assert all(r["password"] in shown for r in csv.DictReader(creds_path("c1").open()))
    _, hidden = run_logged(FakeModal(), learners="2", show_passwords="no")
    assert not any(r["password"] in hidden for r in csv.DictReader(creds_path("c1").open()))


def test_rerun_prints_urls_again_with_the_original_passwords():
    fake = FakeModal()
    launch(fake)
    _, out = run_logged(fake, learners="2")
    rows = list(csv.DictReader(creds_path("c1").open()))
    assert all(r["url"] in out and r["password"] in out for r in rows)


def test_missing_modal_package_is_a_clean_failure_and_dry_run_needs_no_modal(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_modal(name, *a, **k):
        if name == "modal" or name.startswith("modal."):
            raise ImportError("No module named 'modal'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_modal)
    dry = run_pack(RagIde(), "launch", "c1", {"learners": "2"}, dry_run=True)
    assert dry.exit_code == 0 and [i["step"] for i in dry.items] == ["preflight", "image", "learners", "credentials"]
    env = run_pack(RagIde(), "launch", "c1", {"learners": "2"})          # real services, no modal installed
    assert env.exit_code != 0 and "labagent[modal]" in str(env.__dict__)
