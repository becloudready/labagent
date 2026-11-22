"""gpu-notebooks-container: a RunPod pod with RunPod's OWN JupyterLab and its own URL, against a fake RunPod (no network, no key, no cost).

An earlier design installed a second JupyterLab inside the pod and failed on a real pod ("application assets not found"), so this pack installs nothing:
the pod is created with the cloud's Jupyter switched on and a password labagent chose, and the pack reports the URL the cloud gives it.
"""
import csv

import pytest

from labagent.compute.runpod import RunpodProvider
from labagent.packs import registry
from labagent.packs.base import run_pack, state_path
from labagent.packs.gpu_notebooks.container import GpuNotebooksContainer
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, credentials_path
from tests.test_gpu_notebooks import PUB1, home  # noqa: F401  (home is a fixture)
from tests.test_gpu_ssh_keys import ticks
from tests.test_runpod import FakeRunpod


class Ssh:
    """The pack never logs in to the pod now: any attempt is a bug."""

    def __call__(self, *a):
        raise AssertionError("gpu-notebooks-container must not ssh to the machine")


def params(**kw):
    return {"cloud": "runpod", "instance_type": "H100", "vendor": "runpod-secure", "region": "EU-RO-1", "ssh_key_file": "/keys/mine", **kw}


def go(action, api, name="t", p=None, log=None, provider=None, **extra):
    services = {"provider": provider or (lambda pid: RunpodProvider(http=api)), "sleep": lambda s: None, "ssh": Ssh(), "http_get": lambda url, verify: 200,
                "private_fingerprint": lambda path: "x", "public_half": lambda path: PUB1, **extra}
    return run_pack(GpuNotebooksContainer(), action, name, p if p is not None else params(), services=services, log=log)


def test_registered_beside_gpu_notebooks_and_the_dry_run_needs_no_key(home):  # noqa: F811
    assert registry.resolve("gpu-notebooks-container").id == "gpu-notebooks-container"
    assert registry.resolve("container-labs").id == "gpu-notebooks-container"
    assert registry.resolve("gpu-notebooks").id == "gpu-notebooks"
    env = run_pack(GpuNotebooksContainer(), "launch", "t", params(), dry_run=True, services={"provider": lambda _: (_ for _ in ()).throw(AssertionError("no provider"))})
    assert env.status == "ok" and [s["step"] for s in env.to_dict()["items"]] == ["preflight", "host", "url", "report"]


def test_gpu_notebooks_itself_still_skips_containers(home):  # noqa: F811
    api = FakeRunpod()
    env = run_pack(GpuNotebooks(), "launch", "t", {"cloud": "runpod", "instance_type": "H100", "location": "any", "mode": "shared", "labs": 2, "ssh_key_file": "/k"},
                   services={"provider": lambda pid: RunpodProvider(http=api), "sleep": lambda s: None, "ssh": Ssh(), "public_half": lambda p: PUB1,
                             "private_fingerprint": lambda p: "x"})
    assert env.status == "failed" and api.creates() == []


def test_launch_asks_runpod_for_its_own_jupyter_and_reports_the_proxy_url_and_password(home):  # noqa: F811
    api, lines = FakeRunpod(), []
    env = go("launch", api, log=lines.append)
    assert env.status == "ok", env.to_dict()
    (_, _, body), = api.creates()
    row = list(csv.DictReader(credentials_path("t").open()))[0]
    assert body["startJupyter"] is True and body["ports"] == ["22/tcp", "8888/http"]
    assert body["env"]["JUPYTER_PASSWORD"] == row["token"] and len(row["token"]) == 16 and row["token"].isalnum() and body["env"]["PUBLIC_KEY"] == PUB1
    assert row["url"] == "https://pod1-8888.proxy.runpod.net" and row["login_url"] == f"{row['url']}/?token={row['token']}"
    text = "\n".join(lines)
    assert f"URL       {row['url']}" in text and f"Password  {row['token']}" in text and "NOT deleted automatically" in text
    assert "destroy gpu-notebooks-container --name t" in text and "answers" in text


def test_nothing_is_installed_and_the_pod_is_never_logged_in_to(home):  # noqa: F811
    assert go("launch", FakeRunpod()).status == "ok"                  # Ssh() raises if the pack tries


def test_the_password_stays_out_of_state_and_outputs_and_the_file_is_private(home):  # noqa: F811
    env = go("launch", FakeRunpod())
    pw = list(csv.DictReader(credentials_path("t").open()))[0]["token"]
    assert pw not in state_path("gpu-notebooks-container", "t").read_text() and pw not in str(env.to_dict())
    assert env.to_dict()["summary"]["outputs"]["url"] == "https://pod1-8888.proxy.runpod.net"
    assert oct(credentials_path("t").stat().st_mode & 0o777) == "0o600"


def test_show_passwords_no_keeps_it_out_of_the_report(home):  # noqa: F811
    lines = []
    assert go("launch", FakeRunpod(), p=params(show_passwords="no"), log=lines.append).status == "ok"
    pw = list(csv.DictReader(credentials_path("t").open()))[0]["token"]
    assert pw not in "\n".join(lines) and "not shown" in "\n".join(lines)


def test_a_rerun_keeps_the_same_password_and_the_same_pod(home):  # noqa: F811
    api = FakeRunpod()
    assert go("launch", api).status == "ok"
    first = list(csv.DictReader(credentials_path("t").open()))[0]["token"]
    assert go("launch", api).status == "ok"
    assert list(csv.DictReader(credentials_path("t").open()))[0]["token"] == first and len(api.creates()) == 1


def test_a_url_that_never_answers_is_a_warning_not_a_failed_launch(home):  # noqa: F811
    lines = []
    env = go("launch", FakeRunpod(), log=lines.append, http_get=lambda url, verify: 502, clock=ticks())
    assert env.status == "ok" and env.to_dict()["summary"]["outputs"]["public_ok"] is False
    assert "did not answer from here" in "\n".join(lines) and "JupyterLab is starting" in "\n".join(lines)


def test_a_cloud_that_cannot_start_its_own_jupyter_is_refused_before_anything_is_rented(home):  # noqa: F811
    api = FakeRunpod()
    for cloud in ("lambda", "shadeform"):
        env = go("launch", api, p=params(cloud=cloud))
        assert env.status == "failed" and "cannot start its own JupyterLab" in str(env.to_dict())
    assert api.creates() == []


def test_a_failed_url_step_deletes_the_pod(home):  # noqa: F811
    class NoUrl(RunpodProvider):
        def web_url(self, host_id, port):
            return None
    api = FakeRunpod()
    env = go("launch", api, provider=lambda pid: NoUrl(http=api))
    assert env.status == "failed" and "gave no URL" in str(env.to_dict()) and api.deleted() == ["pod1"]


def test_destroy_terminates_the_pod_and_removes_the_password_file(home):  # noqa: F811
    api = FakeRunpod()
    assert go("launch", api).status == "ok" and credentials_path("t").exists()
    env = go("destroy", api)
    assert env.status == "ok" and api.deleted() == ["pod1"] and not credentials_path("t").exists()


@pytest.mark.parametrize("field, value", [("startJupyter", True)])
def test_the_provider_alone_asks_for_jupyter_only_when_given_a_password(field, value):
    from labagent.compute.providers import HostSpec
    api = FakeRunpod()
    base = dict(name="x", instance_type="H100", vendor="runpod-secure", region="EU-RO-1", ssh_public_key=PUB1)
    RunpodProvider(http=api).create(HostSpec(**base))
    assert "startJupyter" not in api.creates()[0][2] and "JUPYTER_PASSWORD" not in api.creates()[0][2]["env"]
    RunpodProvider(http=api).create(HostSpec(jupyter_password="pw", **base))
    assert api.creates()[1][2][field] is value and api.creates()[1][2]["env"]["JUPYTER_PASSWORD"] == "pw"


def test_without_a_region_runpod_picks_the_data_center(home):  # noqa: F811
    api = FakeRunpod()
    p = params()
    p.pop("region")
    env = go("launch", api, p=p)
    assert env.status == "ok", env.to_dict()
    assert "dataCenterIds" not in api.creates()[0][2]
