import csv
import shutil
import subprocess
import urllib.request

import pytest

from labagent.packs.base import PackError, load_state, run_pack
from labagent.packs.rag_ide import local as lc
from labagent.packs.rag_ide.pack import RagIde, creds_path


class FakeLocal:
    def __init__(self, ready_after=0, docker_up=True):
        self.docker_up, self.ready_after, self.polls = docker_up, ready_after, 0
        self.running, self.launches, self.removed, self.volumes_deleted, self.images = {}, [], [], [], []
        self.pauses = []

    def check_auth(self):
        if not self.docker_up:
            raise PackError("docker is installed but its daemon is not running. Start Docker Desktop and retry.")
        return "local docker 29.0"

    def ensure_image(self, image, source): self.images.append((image, source))

    def launch(self, cohort, learner, password, port, **kw):
        c = lc.container_name(cohort, learner)
        self.launches.append({"learner": learner, "password": password, "port": port, **kw})
        self.running[c] = True
        return {"container": c, "url": lc.learner_url(port), "volume": lc.volume_name(cohort, learner), "port": str(port)}

    def ready(self, url):
        self.polls += 1
        return self.polls > self.ready_after

    def alive(self, c): return self.running.get(c, False)
    def find(self, cohort): return [c for c, up in self.running.items() if up]
    def remove(self, c): self.removed.append(c); self.running[c] = False
    def delete_volume(self, v): self.volumes_deleted.append(v)
    def pause(self, s): self.pauses.append(s)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path))


def launch(fake, **params):
    lines = []
    env = run_pack(RagIde(), "launch", "c1", {"platform": "local", "learners": "2", **params}, services={"local": fake}, log=lines.append)
    return env, "\n".join(lines)


def test_launch_starts_each_learner_on_its_own_loopback_port_and_writes_credentials():
    fake = FakeLocal()
    env, out = launch(fake, port=9100, hours=2, idle_minutes=5)
    assert env.exit_code == 0 and [l["port"] for l in fake.launches] == [9100, 9101]
    assert fake.launches[0]["hours"] == 2 and fake.launches[0]["idle_s"] == 300 and fake.images[0][0] == lc.IMAGE
    rows = list(csv.DictReader(creds_path("c1").open()))
    assert [r["url"] for r in rows] == ["http://127.0.0.1:9100/", "http://127.0.0.1:9101/"]
    assert all(r["url"] in out and r["password"] in out for r in rows)                 # 2 learners: passwords shown
    assert creds_path("c1").stat().st_mode & 0o777 == 0o600
    assert not any(r["password"] in str(load_state("rag-ide", "c1")) for r in rows)


def test_docker_not_running_fails_with_the_fix_and_starts_nothing():
    fake = FakeLocal(docker_up=False)
    env, _ = launch(fake)
    assert env.exit_code != 0 and "Start Docker Desktop" in str(env.__dict__) and fake.launches == []


def test_rerun_keeps_running_learners_and_restarts_dead_ones():
    fake = FakeLocal()
    launch(fake)
    first = {r["learner"]: r["password"] for r in csv.DictReader(creds_path("c1").open())}
    fake.running[lc.container_name("c1", "learner-01")] = False
    launch(fake)
    after = {r["learner"]: r["password"] for r in csv.DictReader(creds_path("c1").open())}
    assert len(fake.launches) == 3 and fake.launches[2]["learner"] == "learner-01"
    assert after["learner-02"] == first["learner-02"] and after["learner-01"] != first["learner-01"]


def test_waits_for_ides_and_fails_with_where_to_look():
    fake = FakeLocal(ready_after=3)
    assert launch(fake)[0].exit_code == 0 and fake.pauses
    env, _ = launch(FakeLocal(ready_after=10_000))
    assert env.exit_code != 0 and "docker logs" in str(env.__dict__)


def test_bad_port_and_platform_are_rejected():
    assert launch(FakeLocal(), port=80)[0].exit_code != 0
    assert run_pack(RagIde(), "launch", "c1", {"platform": "docker"}, services={"local": FakeLocal()}).exit_code != 0


def test_destroy_removes_containers_orphans_volumes_and_credentials():
    fake = FakeLocal()
    launch(fake)
    fake.running["labagent-lab-c1-stray"] = True
    env = run_pack(RagIde(), "destroy", "c1", {"platform": "local"}, services={"local": fake})
    assert env.exit_code == 0 and not any(fake.running.values()) and "labagent-lab-c1-stray" in fake.removed
    assert sorted(fake.volumes_deleted) == [lc.volume_name("c1", "learner-01"), lc.volume_name("c1", "learner-02")] and not creds_path("c1").exists()
    f2 = FakeLocal()
    launch(f2)
    run_pack(RagIde(), "destroy", "c1", {"platform": "local", "keep_work": True}, services={"local": f2})
    assert f2.volumes_deleted == []


def test_docker_run_arguments_are_locked_down_and_keep_secrets_off_the_command_line():
    args = lc.run_args("c1", "ana", "pw_secret_12345", 9100, image="img:1", cpu=1.5, memory_mib=2048, hours=3, idle_s=600, env={"GROQ_API_KEY": "gsk_x"})
    joined = " ".join(args)
    assert "127.0.0.1:9100:8080" in joined and "--cap-drop ALL" in joined and "no-new-privileges" in joined
    assert "--memory 2048m" in joined and "--cpus 1.5" in joined and "--pids-limit" in joined
    assert "pw_secret_12345" not in joined and "gsk_x" not in joined and "-e PASSWORD" in joined and "-e GROQ_API_KEY" in joined
    assert "timeout 10800" in joined and "--idle-timeout-seconds 600" in joined and "--auth password" in joined
    assert "--privileged" not in joined and "docker.sock" not in joined


def test_missing_image_without_a_dockerfile_gives_a_clear_error(tmp_path, monkeypatch):
    gw = lc.LocalDockerGateway()
    monkeypatch.setattr(gw, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no such image"))
    with pytest.raises(PackError, match="Dockerfile.lab"):
        gw.ensure_image("nope:1", str(tmp_path))


@pytest.mark.skipif(not shutil.which("docker") or subprocess.run(["docker", "image", "inspect", lc.IMAGE], capture_output=True).returncode != 0,
                    reason="needs a running Docker daemon and the labagent-rag-lab:local image")
def test_real_docker_end_to_end():
    env = run_pack(RagIde(), "launch", "itest", {"platform": "local", "learners": "1", "port": 18190, "hours": 1, "idle_minutes": 5})
    try:
        assert env.exit_code == 0
        row = next(csv.DictReader(creds_path("itest").open()))
        assert urllib.request.urlopen(row["url"] + "login", timeout=10).status == 200
    finally:
        run_pack(RagIde(), "destroy", "itest", {"platform": "local"})
    assert not creds_path("itest").exists()
