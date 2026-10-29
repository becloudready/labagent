"""AML pack, part 3: the RAG mode. Uses the keyword-only embedder and an in-memory store, so no model download and no network."""
import json
from pathlib import Path

import pytest

from labagent.ai.rag import RAGLabClient
from labagent.ai.rag.config import RAGSettings
from labagent.cli import main
from labagent.packs import registry
from labagent.packs.aml.corpus import documents as D
from labagent.packs.aml.corpus.documents import CaseIndex
from labagent.packs.aml.gen import get
from labagent.packs.aml.pack import MARKER, AmlLab
from labagent.packs.aml.rag import quality as Q
from labagent.packs.aml.rag.bundle import client_retriever, evidence_bundle, render
from labagent.packs.aml.rag.qa import policy_qa
from labagent.packs.aml.rag.tools import AmlLab as Lab
from labagent.packs.base import PackContext, load_state, run_pack, state_path
from tests.test_aml_corpus import ECFR, JUSTICE


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LABAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EMBEDDING_MODEL_NAME", "hashing")
    monkeypatch.delenv("CHROMA_PATH", raising=False)
    return tmp_path


def memory_client(out=None):
    return RAGLabClient(vector_db="memory", llm_provider="mock", settings=RAGSettings.from_env())


def services(tmp_path):
    return {"allow_tiny": True, "client": memory_client, "regs_http": lambda url: ECFR if "ECFR" in url else JUSTICE, "regs_cache": tmp_path / "regs"}


def launch(tmp_path, **params):
    base = {"size": "tiny", "out": str(tmp_path / "lab"), "embedder": "hashing"}
    base.update(params)
    return run_pack(AmlLab(), "launch", "t", base, services=services(tmp_path))


@pytest.fixture(scope="module")
def tables():
    return get("builtin").generate(seed=42, size="small", jurisdiction="ca").tables


def built_client(tables, with_regs=False, j="ca"):
    from labagent.packs.aml.corpus.build import build_chunks
    c = memory_client()
    c.ingest_chunks(build_chunks(tables, j, with_regulations=False))
    return c


# ---- the pack ------------------------------------------------------------------------------------------------------------------

def test_pack_is_registered_with_its_catalog_entry(capsys, home):
    assert registry.resolve("aml-rag").id == "aml" and registry.resolve("anti-money-laundering").id == "aml"
    assert main(["packs", "--json"]) == 0
    entry = next(p for p in json.loads(capsys.readouterr().out)["packs"] if p["id"] == "aml")
    assert entry["platforms"] == ["local"] and entry["cost_tier"] == "free"
    assert {"mode", "jurisdiction", "size", "generator", "seed"} <= {q["name"] for q in entry["params"]}


def test_launch_builds_the_lab_folder_and_reports_quality(home):
    env = launch(home)
    assert env.status == "ok", env.error
    out = home / "lab"
    assert (out / MARKER).exists() and (out / "tables" / "transactions.csv").exists() and (out / "data" / "policy-ca.md").exists()
    assert (out / "answer_key" / "ground_truth.csv").exists() and not (out / "tables" / "ground_truth.csv").exists()
    assert (out / "LAB_GUIDE.md").exists() and (out / "aml_tools.py").exists() and (out / "corpus" / "chunks.jsonl").exists()
    o = env.summary["outputs"]
    assert o["mode"] == "rag" and o["counts"]["customers"] == 60 and Path(o["out_dir"]) == out.resolve()
    q = o["quality"]
    assert q["passed"] and q["citation_validity"] == 1.0 and 0 <= q["retrieval_hit_rate"] <= 1 and q["triage_reference_agreement"] >= 0.8
    assert q["keyword_only_embedder"] is True and q["triage_chat_model_agreement"] is None


def test_launch_is_resumable_and_a_second_run_skips_everything(home):
    launch(home)
    again = launch(home)
    assert again.status == "ok" and again.summary["skipped"] == 6 and again.summary["done"] == 0


def test_regulation_text_is_in_the_lab_unless_skipped(home):
    launch(home)
    assert (home / "lab" / "data" / "regulation-pcmltfa.md").exists()
    assert "Open Government Licence - Canada" in (home / "lab" / "data" / "regulation-pcmltfa.md").read_text()
    launch(home, out=str(home / "lab2"), regulations="skip")
    assert not list((home / "lab2" / "data").glob("regulation-*"))
    assert not any(json.loads(l)["meta"]["doc_type"] == "regulation" for l in (home / "lab2" / "corpus" / "chunks.jsonl").read_text().splitlines())


def test_a_network_failure_is_a_clear_step_failure(home):
    def down(url):
        raise OSError("no route")
    env = run_pack(AmlLab(), "launch", "t", {"size": "tiny", "out": str(home / "lab")}, services={**services(home), "regs_http": down})
    assert env.status == "failed" and "regulations=skip" in env.error and "no route" not in env.error


def test_bad_options_fail_early_with_a_plain_message(home):
    for bad, msg in (({"mode": "x"}, "mode"), ({"jurisdiction": "fr"}, "jurisdiction"), ({"seed": "abc"}, "seed"), ({"generator": "magic"}, "generator"),
                     ({"cases": 3}, "cases"), ({"guidance": "x"}, "guidance")):
        env = launch(home, out=str(home / "x"), **bad)
        assert env.status == "failed" and msg in env.error, (bad, env.error)
    env = run_pack(AmlLab(), "launch", "t", {"size": "tiny", "out": str(home / "x")}, services={**services(home), "allow_tiny": False})
    assert "small or medium" in env.error


def test_it_refuses_a_folder_it_did_not_create(home):
    (home / "mine").mkdir()
    (home / "mine" / "notes.txt").write_text("keep")
    env = launch(home, out=str(home / "mine"))
    assert env.status == "failed" and "was not created by this pack" in env.error and (home / "mine" / "notes.txt").read_text() == "keep"


def test_a_failed_quality_gate_stops_the_launch_and_names_the_reason(home, monkeypatch):
    monkeypatch.setattr(Q, "retrieval_check", lambda *a, **k: {"questions": 5, "hits": 0, "hit_rate": 0.0, "misses": []})

    def client(out=None):
        c = memory_client()                    # pretend the embedder understands meaning, so the gate applies
        c.embedder.name = "all-MiniLM-L6-v2"
        return c
    env = run_pack(AmlLab(), "launch", "t", {"size": "tiny", "out": str(home / "lab")}, services={**services(home), "client": client})
    assert env.status == "failed" and "retrieval hit rate 0.00 is below the 0.6 gate" in env.error
    assert "guide" not in load_state("aml", "t")


def test_destroy_removes_only_what_the_pack_wrote(home):
    launch(home)
    (home / "lab" / "my_notes.md").write_text("mine")
    env = run_pack(AmlLab(), "destroy", "t", {"out": str(home / "lab")}, services={})
    assert env.status == "ok"
    assert [p.name for p in (home / "lab").iterdir()] == ["my_notes.md"] and not state_path("aml", "t").exists()
    launch(home, out=str(home / "lab3"))
    run_pack(AmlLab(), "destroy", "t", {"out": str(home / "lab3")}, services={})
    assert not (home / "lab3").exists()                      # nothing of the learner's was there, so the folder goes too


def test_destroy_leaves_alone_a_folder_without_the_marker(home):
    (home / "other").mkdir()
    (home / "other" / "tables").mkdir()
    env = run_pack(AmlLab(), "destroy", "t", {"out": str(home / "other")}, services={})
    assert env.status == "failed" and (home / "other" / "tables").exists()


def test_envelope_carries_no_secret_or_answer(home):
    env = launch(home)
    text = json.dumps(env.to_dict())
    state = json.dumps(load_state("aml", "t"))
    for g in get("builtin").generate(seed=42, size="tiny").tables["ground_truth"][:5]:
        assert g["reason"] not in text and g["reason"] not in state


# ---- the quality library --------------------------------------------------------------------------------------------------------

def test_evidence_bundle_has_every_part_and_never_reads_the_alerts_own_answer(tables):
    blind = {k: v for k, v in tables.items() if k != "ground_truth"}
    c = built_client(tables)
    aid = tables["alerts"][20]["id"]
    b = evidence_bundle(blind, aid, client_retriever(c), "ca", with_regulations=False)
    assert set(b) >= {"alert", "customer", "kyc", "sanctions", "transactions", "policy", "regulations", "similar_cases", "uncommon_parties"}
    assert b["policy"] and all(p["metadata"]["doc_type"] == "policy" and p["metadata"]["jurisdiction"] == "ca" for p in b["policy"])
    assert b["similar_cases"] == []                                        # without an answer key there is nothing to recall
    full = evidence_bundle(tables, aid, client_retriever(c), "ca", with_regulations=False)
    assert all(s["date"] < b["alert"]["date"] for s in full["similar_cases"]) and aid not in {s["alert_id"] for s in full["similar_cases"]}
    assert aid in render(full) and "ALERT" in render(full)


def test_citation_check_catches_invented_ids_and_sections(tables):
    real = tables["transactions"][100]["id"]
    ok = Q.citation_validity(f"See {real}, PCMLTFA s. 7 and policy section 8.", tables)
    assert ok["rate"] == 1.0 and ok["cited"] == 3
    bad = Q.citation_validity(f"See {real}, T9999999, PCMLTFA s. 777 and policy section 40.", tables)
    assert bad["rate"] == 0.25 and set(bad["invalid"]) == {"T9999999", "PCMLTFA 777", "policy section 40"}
    assert Q.citation_validity("Nothing cited here.", tables)["rate"] == 1.0


def test_reference_rule_agrees_with_the_answer_key_on_many_datasets():
    right = total = 0
    for seed in (42, 1, 2, 3):
        for j in ("ca", "both"):
            t = get("builtin").generate(seed=seed, size="small", jurisdiction=j).tables
            ix = CaseIndex(t)
            for g in t["ground_truth"]:
                b = evidence_bundle(t, g["alert_id"], lambda q, k, w: [], j, ix, with_regulations=False)
                right += Q.triage_baseline(b) == g["label"]
                total += 1
    assert right / total >= 0.97


def test_sample_alerts_are_balanced(tables):
    ids = Q.sample_alerts(tables, 4)
    truth = {g["alert_id"]: g["label"] for g in tables["ground_truth"]}
    assert len(ids) == 12 and sorted(truth[i] for i in ids).count("monitor") == 4


def test_chat_model_triage_is_scored_the_same_way(tables):
    class Res:
        text = "I would escalate this one. T0000001, policy section 8."

    class Llm:
        def complete(self, prompt, context):
            return Res()

    c = built_client(tables)
    c.llm = Llm()
    r = Q.triage_check(tables, client_retriever(c), "ca", False, client=c, per_label=2)
    assert r["chat_model"]["alerts"] == 6 and r["chat_model"]["agreement"] == pytest.approx(2 / 6, abs=0.01)     # says escalate every time


def test_qa_set_covers_both_countries_and_skips_regulations_when_asked():
    both = policy_qa("both", True)
    assert {c["expect"]["jurisdiction"] for c in both} == {"ca", "us"} and any("citation" in c["expect"] for c in both)
    assert not any("citation" in c["expect"] for c in policy_qa("ca", False))


# ---- the learner helper ---------------------------------------------------------------------------------------------------------

def test_learner_helper_works_from_the_lab_folder(home):
    launch(home)
    lab = Lab(home / "lab", client=memory_client())
    lab.client.ingest_chunks([json.loads(l) for l in (home / "lab" / "corpus" / "chunks.jsonl").read_text().splitlines() if l.strip()])
    assert len(lab.alerts()) > 10 and "label" not in lab.alerts()[0]
    r = lab.ask("When must a suspicious transaction report be filed?", doc_type="policy")
    assert r.chunks and all(c.metadata["doc_type"] == "policy" for c in r.chunks)
    b = lab.evidence(lab.alerts()[3]["id"])
    assert b["transactions"] and b["policy"]
    first = lab.alerts()[0]["id"]
    truth = {g["alert_id"]: g["label"] for g in lab.tables["ground_truth"]}
    assert lab.score({first: truth[first]})["agreement"] == 1.0
    wrong = lab.score({first: "dismiss" if truth[first] != "dismiss" else "escalate"})
    assert wrong["agreement"] == 0.0 and wrong["disagree"][first]["key"] == truth[first]
    assert lab.check(f"See {lab.tables['transactions'][0]['id']} and T9999999.")["invalid"] == ["T9999999"]


def test_the_guide_names_real_commands_and_follows_the_writing_rules(home):
    launch(home)
    text = (home / "lab" / "LAB_GUIDE.md").read_text()
    for needle in ("lab.ask(", "lab.show(", "lab.check(", "lab.score(", "labagent destroy aml --name t", "keyword-only fallback"):
        assert needle in text
    assert "—" not in text and "–" not in text
    assert "rag-ide" not in text


def test_open_client_builds_the_real_client_and_rebuilds_an_empty_index(home):
    """Every other test injects a client. This one runs the helper's own import and rebuild path, which once failed on a bad relative import."""
    launch(home)
    from labagent.packs.aml.rag import tools
    client = tools.open_client(home / "lab")
    assert client.store.count() > 20
    lab = Lab(home / "lab")
    assert lab.ask("When must a suspicious transaction report be filed?", doc_type="policy").chunks
