"""What the console needs from gpu-notebooks: pick a machine by card and area, no vendor or region; sealed lab tokens; jobs that stay safe."""
import base64
import csv

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from labagent.compute import providers
from labagent.compute.providers import OfferQuery, ShadeformProvider, area_of, best_os
from labagent.packs.base import load_state, run_pack
from labagent.packs.gpu_notebooks.pack import GpuNotebooks, credentials_path
from labagent.run import run_pack_job
from tests.test_gpu_notebooks import FakeShadeform, FakeSsh, home, svc  # noqa: F401  (home is a fixture)


def shape(vendor, itype, cents, regions, kind="vm", oses=("ubuntu22.04_cuda12.8_shade_os",), gpu="H100"):
    return {"cloud": vendor, "shade_instance_type": itype, "hourly_price": cents, "deployment_type": kind, "gpu_type": gpu, "num_gpus": 1,
            "configuration": {"os_options": list(oses), "vram_per_gpu_in_gb": 80},
            "availability": [{"region": r, "display_name": n, "available": a, "rental_type": "on_demand"} for r, n, a in regions]}


TYPES = {"instance_types": [
    shape("massedcompute", "H100", 273, [("desmoines-usa-1", "US, Des Moines, IA", True)], oses=("ubuntu22.04_cuda13.0_shade_os",)),
    shape("scaleway", "H100", 330, [("paris-france-1", "FR, Paris", True), ("warsaw-poland-1", "PL, Warsaw", True)],
          oses=("ubuntu24.04_cuda12.8_shade_os", "ubuntu24.04_cuda13.0_shade_os")),
    shape("hyperstack", "H100", 250, [("montreal-canada-2", "CA, Montreal", False)]),                    # cheapest, but sold out
    shape("someone", "H100", 100, [("tokyo-japan-2", "JP, Tokyo", True)], kind="container"),              # cheapest, but a container
    shape("excesssupply", "RTX5090", 65, [("oslo-norway-3", "NO, Central", True)], gpu="RTX5090"),
]}


class Api(FakeShadeform):
    def __call__(self, method, path, body=None):
        if path.startswith("/v1/instances/types"):
            self.calls.append((method, path, body))
            return TYPES
        return super().__call__(method, path, body)


def params(**kw):
    return {"instance_type": "H100", "location": "europe", "ssh_key_id": "key-1", "login_user": "", **kw}


def launch(api, name="t", given=None, **extra):
    return run_pack(GpuNotebooks(), "launch", name, given if given is not None else params(), services=svc(api, **extra))


# ---- areas ----

@pytest.mark.parametrize("name,area", [("US, Dallas, TX", "north-america"), ("CA, Montreal", "north-america"), ("FR, Paris", "europe"),
                                       ("GB, London", "europe"), ("JP, Tokyo", "asia-pacific"), ("IN, Mumbai", "asia-pacific"),
                                       ("IL, Jerusalem", ""), ("ca-east-1", ""), ("", ""), ("us, dallas", "")])
def test_area_comes_from_the_country_code_and_is_empty_when_unknown(name, area):
    assert area_of(name) == area


def test_the_newest_cuda_image_wins_then_the_newest_ubuntu():
    assert best_os(["ubuntu22.04_cuda13.0_shade_os", "ubuntu24.04_cuda12.8_shade_os"]) == "ubuntu22.04_cuda13.0_shade_os"
    assert best_os(["ubuntu22.04_cuda13.0_shade_os", "ubuntu24.04_cuda13.0_shade_os", "ubuntu22.04"]) == "ubuntu24.04_cuda13.0_shade_os"
    assert best_os(["ubuntu22.04", "windows"]) == ""


def test_the_offer_query_can_be_narrowed_to_an_area():
    p = ShadeformProvider(http=Api())
    assert {o.region for o in p.offers(OfferQuery(area="europe"))} == {"paris-france-1", "warsaw-poland-1", "oslo-norway-3"}
    assert {o.region for o in p.offers(OfferQuery(area="north-america", available_only=False))} == {"desmoines-usa-1", "montreal-canada-2"}
    assert len(p.offers(OfferQuery(area="any"))) == len(p.offers(OfferQuery()))


# ---- picking a machine ----

def test_location_picks_the_cheapest_available_machine_there_and_the_best_image(home):
    api = Api()
    env = launch(api)
    assert env.status == "ok", env.error
    body = api.creates()[0][2]
    assert (body["cloud"], body["region"], body["os"]) == ("scaleway", "paris-france-1", "ubuntu24.04_cuda13.0_shade_os")
    out = env.summary["outputs"]
    assert (out["vendor"], out["region"]) == ("scaleway", "paris-france-1")


def test_any_location_takes_the_cheapest_in_the_world_but_never_sold_out_or_a_container(home):
    api = Api()
    assert launch(api, given=params(location="any")).status == "ok"
    assert api.creates()[0][2]["cloud"] == "massedcompute"             # $2.73: the $1.00 one is a container, the $2.50 one is sold out


def test_a_card_is_a_shape_and_nothing_else_is_taken_instead(home):
    api = Api()
    env = launch(api, given=params(instance_type="RTX5090", mode="shared", labs=2))
    assert env.status == "ok", env.error
    assert api.creates()[0][2]["shade_instance_type"] == "RTX5090" and api.creates()[0][2]["cloud"] == "excesssupply"


def test_nothing_available_in_the_area_is_a_plain_message_and_creates_nothing(home):
    api = Api()
    env = launch(api, given=params(instance_type="RTX5090", mode="shared", labs=2, location="north-america"))
    assert env.status == "failed" and api.creates() == []
    assert "no RTX5090 is available in North America right now" in env.error and "labagent compute offers --cloud shadeform --type RTX5090" in env.error


def test_a_rerun_uses_the_same_machine_and_creates_nothing_twice(home):
    api = Api()
    assert launch(api).status == "ok"
    first = load_state("gpu-notebooks", "t")
    assert (first["vendor"], first["region"], first["os"]) == ("scaleway", "paris-france-1", "ubuntu24.04_cuda13.0_shade_os")
    assert launch(api).status == "ok" and len(api.creates()) == 1


def test_an_explicit_vendor_and_region_are_never_replaced(home):
    api = Api()
    env = launch(api, given=params(vendor="massedcompute", region="desmoines-usa-1", location="europe"))
    assert env.status == "ok" and api.creates()[0][2]["cloud"] == "massedcompute"


def test_without_location_the_old_behaviour_stays(home):
    env = launch(Api(), given={"instance_type": "H100", "ssh_key_id": "key-1", "login_user": ""})
    assert env.status == "failed" and "set vendor and region" in env.error


def test_an_unknown_location_is_refused(home):
    env = launch(Api(), given=params(location="mars"))
    assert env.status == "failed" and "location must be one of: any, europe, north-america, asia-pacific" in env.error


def test_max_price_ignores_dearer_offers_and_says_so_when_none_is_left(home):
    api = Api()
    env = launch(api, given=params(location="any", max_price=2.8))
    assert env.status == "ok" and api.creates()[0][2]["cloud"] == "massedcompute"            # $2.73 is under the ceiling
    env = launch(Api(), name="u", given=params(location="any", max_price=2.5))
    assert env.status == "failed" and "at $2.50/h or less" in env.error
    assert launch(Api(), name="v", given=params(max_price="cheap")).status == "failed"
    assert launch(Api(), name="w", given=params(max_price=-1)).status == "failed"


# ---- nothing secret in the printed report when the console asks for that ----

def test_show_passwords_no_keeps_every_token_and_the_shell_password_out_of_the_report(home):
    lines = []
    env = run_pack(GpuNotebooks(), "launch", "t", params(labs=2, show_passwords="no", login_user="labagent"), services=svc(Api()), log=lines.append)
    assert env.status == "ok", env.error
    tokens = [r["token"] for r in csv.DictReader(credentials_path("t").open())]
    shown = "\n".join(lines)
    assert len(tokens) == 2 and all(t not in shown for t in tokens) and "?token=" not in shown
    assert "https://203-0-113-7.sslip.io/lab-01/lab" in shown and "password: (not shown)" in shown
    assert all(t in credentials_path("t").read_text() for t in tokens)                        # still in the file


def test_the_default_still_prints_the_links_with_their_tokens(home):
    lines = []
    run_pack(GpuNotebooks(), "launch", "t", params(labs=1), services=svc(Api()), log=lines.append)
    assert "?token=" in "\n".join(lines)
    assert launch(Api(), name="x", given=params(show_passwords="maybe")).status == "failed"


# ---- the tokens come back sealed, one per lab ----

def test_a_job_with_a_public_key_gets_every_lab_token_sealed_and_the_cli_gets_none(home):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    api = Api()
    env = run_pack(GpuNotebooks(), "launch", "t", params(labs=3), services=svc(api), pubkey_pem=pem)
    assert env.status == "ok", env.error
    assert [s["user"] for s in env.secrets] == ["lab-01", "lab-02", "lab-03"]
    tokens = {r["lab"]: r["token"] for r in csv.DictReader(credentials_path("t").open())}
    for s in env.secrets:
        plain = priv.decrypt(base64.b64decode(s["ciphertext"]), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        assert plain.decode() == tokens[s["user"]]
    assert all(t not in str(env.to_dict().get("summary")) for t in tokens.values())          # never in the plain part of the result
    assert run_pack(GpuNotebooks(), "launch", "u", params(labs=3), services=svc(Api())).secrets == []         # no key, no secrets


# ---- what a remote job may set ----

def test_a_remote_job_cannot_point_labagent_at_a_key_file_on_the_runner(home):
    job = {"run_id": "r1", "command": "launch", "pack": "gpu-notebooks", "name": "demo", "mode": "dry-run"}
    assert run_pack_job({**job, "params": {"location": "europe", "mig_profile": "1g.10gb", "ssh_key_id": "key-1"}}).status == "ok"
    refused = run_pack_job({**job, "params": {"ssh_key_file": "/etc/passwd"}})
    assert refused.status == "failed" and "ssh_key_file" in refused.error
