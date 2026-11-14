"""The VM script's `partition`, run for real by bash against a stub `nvidia-smi` that behaves like an H100, an A100 or an RTX 5090.

The stub keeps its GPUs in a JSON file, so a test can also "reboot" the machine. Nothing here touches a real GPU or a cloud.
"""
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from labagent.packs.gpu_notebooks import providers

SCRIPT = Path(providers.__file__).with_name("setup.sh")
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")

NVIDIA_SMI = r'''#!__PY__
import json, os, sys, uuid
P = os.environ["NVSTUB_STATE"]
st = json.load(open(P))
gpus, a = st["gpus"], sys.argv[1:]


def save():
    json.dump(st, open(P, "w"))


def only():
    return [gpus[int(a[a.index("-i") + 1])]] if "-i" in a else gpus


def idx(g):
    return gpus.index(g)


if a == ["-L"]:
    for i, g in enumerate(gpus):
        print(f"GPU {i}: {g['name']} (UUID: {g['uuid']})")
        for j, s in enumerate(g["slices"]):
            print(f"  MIG {s['profile']:<10} Device {j:>2}: (UUID: {s['uuid']})")
elif any(x.startswith("--query-gpu=") for x in a):
    field = next(x for x in a if x.startswith("--query-gpu="))[len("--query-gpu="):]
    for g in only():
        if field == "index":
            print(idx(g))
        elif field == "uuid":
            print(g["uuid"])
        elif field == "name":
            print(g["name"])
        elif field == "mig.mode.current":
            print(g["current"] if g["mig"] else "[N/A]")
        elif field == "mig.mode.pending":
            print(g["pending"] if g["mig"] else "[N/A]")
elif "-mig" in a:
    g = only()[0]
    want = "Enabled" if a[a.index("-mig") + 1] == "1" else "Disabled"
    if not g["mig"]:
        print("Unable to enable MIG Mode for GPU: Not Supported", file=sys.stderr)
        sys.exit(3)
    g["pending"] = want
    if not st["needs_reset"]:
        g["current"] = want
    save()
elif "--gpu-reset" in a:
    if not st["reset_works"]:
        print("GPU reset is not supported on this VM", file=sys.stderr)
        sys.exit(4)
    for g in only():
        g["current"] = g["pending"]
    save()
elif a and a[0] == "mig":
    if "-lgip" in a:
        on = [g for g in gpus if g["mig"] and g["current"] == "Enabled"]
        if not on:
            print("No MIG-enabled devices found.")
            sys.exit(6)
        for g in on:
            for name, n in st["profiles"]:
                print(f"|{idx(g):>4}  MIG {name:<14} 19     {n}/{n}        4.75       No     14     0     0   |")
    elif "-dci" in a or "-dgi" in a:
        for g in gpus:
            g["slices"] = []
        save()
    elif "-cgi" in a:
        g, name = only()[0], a[a.index("-cgi") + 1]
        limit = dict(st["profiles"]).get(name)
        if limit is None or sum(1 for s in g["slices"] if s["profile"] == name) >= limit:
            print("Unable to create a GPU instance", file=sys.stderr)
            sys.exit(5)
        g["slices"].append({"profile": name, "uuid": "MIG-" + str(uuid.uuid4())})
        save()
else:
    print("stub: unsupported " + " ".join(a), file=sys.stderr)
    sys.exit(99)
'''

DOCKER = r'''#!/bin/sh
case "$1" in
  info) echo " Runtimes: io.containerd.runc.v2 nvidia runc" ;;
esac
exit 0
'''

PROFILES = {
    "H100-80": [["1g.10gb", 7], ["1g.10gb+me", 1], ["1g.20gb", 4], ["2g.20gb", 3], ["3g.40gb", 2], ["4g.40gb", 1], ["7g.80gb", 1]],
    "A100-40": [["1g.5gb", 7], ["1g.5gb+me", 1], ["2g.10gb", 3], ["3g.20gb", 2], ["4g.20gb", 1], ["7g.40gb", 1]],
    "A100-80": [["1g.10gb", 7], ["1g.10gb+me", 1], ["1g.20gb", 4], ["2g.20gb", 3], ["3g.40gb", 2], ["4g.40gb", 1], ["7g.80gb", 1]],
}
NAMES = {"H100-80": "NVIDIA H100 PCIe", "A100-40": "NVIDIA A100-SXM4-40GB", "A100-80": "NVIDIA A100 80GB PCIe", "RTX5090": "NVIDIA GeForce RTX 5090"}


class Box:
    """A fake GPU machine: stubs on PATH, state in a file."""

    def __init__(self, tmp: Path, model: str, gpus: int = 1, needs_reset: bool = False, reset_works: bool = True, mig_on: bool = False):
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "nvidia-smi").write_text(NVIDIA_SMI.replace("__PY__", sys.executable))
        (self.bin / "docker").write_text(DOCKER)
        for f in self.bin.iterdir():
            f.chmod(0o755)
        self.state_file = tmp / "gpus.json"
        capable = model != "RTX5090"
        self.state_file.write_text(json.dumps({
            "profiles": PROFILES.get(model, []), "needs_reset": needs_reset, "reset_works": reset_works,
            "gpus": [{"name": NAMES[model], "uuid": f"GPU-{uuid.uuid4()}", "mig": capable, "current": "Enabled" if mig_on else "Disabled",
                      "pending": "Enabled" if mig_on else "Disabled", "slices": []} for _ in range(gpus)]}))

    def state(self):
        return json.loads(self.state_file.read_text())

    def reboot(self):
        """What a reboot does: pending MIG mode takes effect, and the slices are gone."""
        st = self.state()
        for g in st["gpus"]:
            g["current"], g["slices"] = g["pending"], []
        self.state_file.write_text(json.dumps(st))

    def run(self, sub="partition", **env):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "NVSTUB_STATE": str(self.state_file), "HOME": str(self.bin)}
        e.update({k: v for k, v in env.items()})
        r = subprocess.run(["bash", str(SCRIPT), sub], env=e, cwd=self.bin, capture_output=True, text=True, encoding="utf-8", timeout=60)
        r.facts = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l and l.split("=", 1)[0].isupper())
        return r


@pytest.fixture
def box(tmp_path):
    return lambda model, **kw: Box(tmp_path, model, **kw)


# ---- the profile: auto ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("model,profile,per_gpu", [("H100-80", "1g.10gb", 7), ("A100-80", "1g.10gb", 7), ("A100-40", "1g.5gb", 7)])
def test_auto_picks_the_smallest_slice_the_gpu_offers(box, model, profile, per_gpu):
    r = box(model).run(PROFILE="auto")
    assert r.returncode == 0, r.stderr
    assert (r.facts["PROFILE"], r.facts["SLICES"], r.facts["GPU_COUNT"]) == (profile, str(per_gpu), "1")


def test_auto_is_the_default_and_never_picks_a_variant_with_extras(box):
    b = box("A100-40")
    r = b.run()                                                                 # no PROFILE in the environment at all
    assert r.facts["PROFILE"] == "1g.5gb"                                       # not 1g.5gb+me
    assert {s["profile"] for g in b.state()["gpus"] for s in g["slices"]} == {"1g.5gb"}


def test_eight_gpus_give_fifty_six_slices(box):
    assert box("A100-80", gpus=8).run().facts["SLICES"] == "56"


def test_an_explicit_profile_is_honoured(box):
    r = box("H100-80").run(PROFILE="2g.20gb")
    assert (r.facts["PROFILE"], r.facts["SLICES"]) == ("2g.20gb", "3")


def test_a_profile_the_gpu_does_not_have_is_a_plain_error(box):
    r = box("A100-40").run(PROFILE="1g.10gb")                                   # the old H100 default, on a 40 GB card
    assert r.returncode != 0 and "no MIG slice of profile 1g.10gb could be created" in r.stderr and "nvidia-smi mig -lgip" in r.stderr


def test_running_again_keeps_the_slices_and_a_new_profile_replaces_them(box):
    def slice_ids():
        return {s["uuid"] for g in b.state()["gpus"] for s in g["slices"]}
    b = box("H100-80")
    b.run()
    first = slice_ids()
    assert len(first) == 7
    b.run()
    assert slice_ids() == first                                                 # nothing was recreated
    r = b.run(PROFILE="3g.40gb")
    assert r.facts["SLICES"] == "2" and not first & slice_ids()                 # a different size replaces them


# ---- A100: MIG turns on only after a reset or a reboot ------------------------------------------------------------------

def test_a100_that_resets_its_gpu_carries_on_without_a_reboot(box):
    r = box("A100-40", needs_reset=True, reset_works=True).run()
    assert r.returncode == 0 and r.facts["SLICES"] == "7" and "NEEDS_REBOOT" not in r.stdout


def test_a100_that_cannot_reset_asks_for_exactly_one_reboot_and_then_works(box):
    b = box("A100-40", needs_reset=True, reset_works=False)
    r = b.run()
    assert r.returncode == 75 and r.facts == {"NEEDS_REBOOT": "1"} and "takes effect only after the VM reboots" in r.stderr
    assert b.state()["gpus"][0]["pending"] == "Enabled"                         # it was asked to turn MIG on
    b.reboot()
    r = b.run()
    assert r.returncode == 0 and r.facts["SLICES"] == "7" and r.facts["PROFILE"] == "1g.5gb"


def test_every_gpu_in_the_box_is_switched_on_before_any_slice_is_made(box):
    b = box("A100-80", gpus=8, needs_reset=True, reset_works=True)
    r = b.run()
    assert r.returncode == 0 and r.facts["SLICES"] == "56"
    assert {g["current"] for g in b.state()["gpus"]} == {"Enabled"}


# ---- GPUs without MIG: the RTX 5090 --------------------------------------------------------------------------------------

def test_a_geforce_card_says_plainly_it_has_no_mig_and_what_to_do(box):
    r = box("RTX5090").run()
    assert r.returncode != 0
    assert "has no MIG" in r.stderr and "GeForce RTX 5090" in r.stderr and "mode=shared and labs=N" in r.stderr


def test_shared_mode_touches_no_mig_and_reports_the_whole_gpus(box):
    b = box("RTX5090", gpus=2)
    r = b.run(MODE="shared")
    assert r.returncode == 0, r.stderr
    assert r.facts == {"SLICES": "0", "PROFILE": "shared", "GPU_NAME": "NVIDIA GeForce RTX 5090", "GPU_COUNT": "2"}
    assert all(not g["slices"] for g in b.state()["gpus"])


def test_shared_devices_are_the_gpus_and_mig_devices_are_the_slices(box):
    b = box("RTX5090", gpus=2)
    r = b.run("devices", MODE="shared")
    assert r.stdout.split() == [g["uuid"] for g in b.state()["gpus"]]


def test_mig_devices_are_the_slices(box):
    b = box("H100-80")
    b.run()
    assert b.run("devices", MODE="mig").stdout.split() == [s["uuid"] for s in b.state()["gpus"][0]["slices"]]


def test_shared_mode_refuses_a_gpu_that_still_has_mig_on(box):
    r = box("A100-80", mig_on=True).run(MODE="shared")
    assert r.returncode != 0 and "mode=shared needs it off" in r.stderr and "-mig 0" in r.stderr


def test_a_name_in_the_error_cannot_end_the_script_early(box):
    """A GPU name comes from the driver, but the message is built with it inside double quotes: it must stay one message."""
    b = box("RTX5090")
    st = b.state()
    st["gpus"][0]["name"] = 'NVIDIA "$(touch pwned)" `touch pwned2` RTX'
    b.state_file.write_text(json.dumps(st))
    r = b.run()
    assert r.returncode != 0 and "has no MIG" in r.stderr
    assert not (b.bin / "pwned").exists() and not (b.bin / "pwned2").exists()
