import io
import json
import urllib.error

import pytest

from labagent import cli, nl
from labagent.packs import registry

PACKS = registry.all_packs()


def ask(text, reply):
    return nl.translate(text, PACKS, registry.resolve, chat=lambda *a, **k: reply if isinstance(reply, str) else json.dumps(reply))


def test_the_example_request_becomes_the_right_command():
    plan = ask("Run and deploy rag-tutorial in databricks", {"action": "launch", "pack": "rag-tutorial", "name": None, "params": {}})
    assert plan.command_line() == "labagent launch rag-sec-tutorial --name demo"          # alias resolved, default name applied


def test_tolerates_code_fences_and_chatter_around_the_json():
    raw = 'Sure! ```json\n{"action": "launch", "pack": "rag-sec-tutorial", "name": null, "params": {}}\n``` hope that helps'
    assert ask("deploy rag-tutorial", raw).pack == "rag-sec-tutorial"


def test_name_and_params_are_read_from_the_sentence_not_from_the_model():
    plan = ask("deploy rag-tutorial called team1 with profile MYPROFILE",
               {"action": "launch", "pack": "rag-tutorial", "name": "prod", "params": {"dataset": "evil", "profile": "evil"}})
    assert plan.name == "team1" and plan.params == {"profile": "MYPROFILE"}             # the model's name and params are ignored
    bare = ask("deploy rag-tutorial", {"action": "launch", "pack": "rag-tutorial", "name": "prod", "params": {"profile": "x"}})
    assert bare.name == "demo" and bare.params == {}
    ide = ask("launch rag-ide called cohort1 with learners 5 and platform ec2", {"action": "launch", "pack": "rag-ide"})
    assert ide.name == "cohort1" and ide.params == {"learners": "5", "platform": "ec2"}       # several parameters, read by exact rules


def test_synonym_actions_are_normalised_and_the_sentence_has_the_last_word():
    assert ask("create rag-tutorial", {"action": "create", "pack": "rag-tutorial"}).action == "launch"
    assert ask("remove rag-tutorial", {"action": "remove", "pack": "rag-tutorial"}).action == "destroy"
    assert ask("deploy rag-tutorial", {"action": "gibberish", "pack": "rag-tutorial"}).action == "launch"


def test_a_pack_the_model_guessed_must_be_backed_by_the_sentence():
    with pytest.raises(nl.NLError, match="which pack"):
        ask("deploy it", {"action": "launch", "pack": "rag-sec-tutorial"})
    with pytest.raises(nl.NLError, match="which pack"):
        ask("run and start the rag app", {"action": "launch", "pack": "rag-sec-tutorial"})        # 'rag' and 'app' are too generic to back a guess
    assert ask("start the tutorial notebooks", {"action": "launch", "pack": "rag-sec-tutorial"}).pack == "rag-sec-tutorial"
    assert ask("spin up the text to sql agent", {"action": "launch", "pack": "db-agent"}).pack == "db-agent"


def test_compound_requests_are_refused_not_half_executed():
    with pytest.raises(nl.NLError, match="two things"):
        ask("deploy rag-tutorial and then delete everything", {"action": "launch", "pack": "rag-tutorial"})


def test_a_pack_the_user_typed_beats_the_models_pick():
    plan = ask("deploy rag-tutorial", {"action": "launch", "pack": "db-agent"})
    assert plan.pack == "rag-sec-tutorial" and plan.warnings


def test_destroy_needs_an_explicit_destructive_request():
    with pytest.raises(nl.NLError, match="does not ask to delete"):
        ask("deploy rag-tutorial", {"action": "destroy", "pack": "rag-tutorial", "name": None, "params": {}})
    with pytest.raises(nl.NLError, match="sounds like a deletion"):
        ask("tear down rag-tutorial", {"action": "launch", "pack": "rag-tutorial", "name": None, "params": {}})
    ok = ask("delete rag-tutorial", {"action": "destroy", "pack": "rag-tutorial", "name": None, "params": {}})
    assert ok.action == "destroy" and ok.command_line() == "labagent destroy rag-sec-tutorial --name demo"


def test_unknown_pack_ambiguous_pack_and_junk_are_refused():
    with pytest.raises(nl.NLError, match="not a known pack"):
        ask("deploy nothing-real", {"action": "launch", "pack": "nothing-real", "name": None, "params": {}})
    with pytest.raises(nl.NLError, match="ambiguous"):
        ask("deploy rag", {"action": "launch", "pack": "rag", "name": None, "params": {}})
    with pytest.raises(nl.NLError, match="usable command"):
        ask("deploy rag-tutorial", "I cannot do that")
    with pytest.raises(nl.NLError, match="launch, destroy or list"):
        ask("rag-tutorial please", {"action": "rm -rf /", "pack": "rag-tutorial"})          # no verb in the sentence, nonsense from the model
    assert ask("deploy rag-tutorial", {"action": "rm -rf /", "pack": "rag-tutorial"}).command_line() == "labagent launch rag-sec-tutorial --name demo"


def test_shell_metacharacters_are_quoted_in_the_displayed_command():
    plan = ask("deploy rag-tutorial with profile x&y", {"action": "launch", "pack": "rag-tutorial"})
    assert plan.argv()[-1] == "profile=x&y" and "'profile=x&y'" in plan.command_line()


def test_list_needs_no_pack():
    assert ask("what can you deploy?", {"action": "list"}).argv() == ["packs"]


def test_prompt_is_small_lists_real_packs_and_asks_only_for_action_and_pack():
    msgs = nl.build_messages("deploy rag-tutorial", PACKS)
    sys_prompt = msgs[0]["content"]
    assert "rag-sec-tutorial" in sys_prompt and "rag-tutorial" in sys_prompt and "db-agent" in sys_prompt and "rag-ide" in sys_prompt
    assert "idle_minutes" not in sys_prompt and "profile" not in sys_prompt            # parameters are not the model's job
    assert len(sys_prompt) < 2500                                               # short enough for a 0.5b model to follow
    assert msgs[1]["content"] == "Request: deploy rag-tutorial"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_call_llm_uses_the_openai_shape_and_default_local_endpoint(monkeypatch):
    seen = {}

    def opener(req, timeout):
        seen.update(url=req.full_url, body=json.loads(req.data), auth=req.headers.get("Authorization"))
        return Resp(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())
    assert nl.call_llm(nl.DEFAULT_URL, nl.DEFAULT_MODEL, [{"role": "user", "content": "x"}], opener=opener) == "{}"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions" and seen["auth"] is None
    assert seen["body"]["model"] == "qwen2.5-coder:0.5b" and seen["body"]["temperature"] == 0


def test_call_llm_retries_without_json_mode_and_gives_helpful_errors():
    calls = []

    def opener(req, timeout):
        body = json.loads(req.data)
        calls.append("response_format" in body)
        if "response_format" in body:
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"response_format unsupported"))
        return Resp(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
    assert nl.call_llm("http://x/v1", "m", [], opener=opener) == "ok" and calls == [True, False]

    def down(req, timeout):
        raise urllib.error.URLError("refused")
    with pytest.raises(nl.NLError, match="ollama pull qwen2.5-coder:0.5b"):
        nl.call_llm(nl.DEFAULT_URL, nl.DEFAULT_MODEL, [], opener=down)

    def missing(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"error":"model not found"}'))
    with pytest.raises(nl.NLError, match="ollama pull m"):
        nl.call_llm("http://localhost:11434/v1", "m", [], opener=missing)


def test_settings_precedence_and_locality(monkeypatch):
    monkeypatch.delenv("LABAGENT_LLM_URL", raising=False)
    monkeypatch.delenv("LABAGENT_LLM_MODEL", raising=False)
    assert nl.settings(None, None)["model"] == "qwen2.5-coder:0.5b"
    monkeypatch.setenv("LABAGENT_LLM_MODEL", "other")
    assert nl.settings(None, None)["model"] == "other" and nl.settings(None, "flag")["model"] == "flag"
    assert nl.is_local("http://localhost:11434/v1") and not nl.is_local("https://api.example.com/v1")


# -- CLI wiring ------------------------------------------------------------------
@pytest.fixture
def wired(monkeypatch):
    ran = []
    monkeypatch.setattr(nl, "call_llm", lambda *a, **k: json.dumps({"action": "launch", "pack": "rag-tutorial", "name": None, "params": {}}))
    real = cli.main

    def fake_main(argv=None):
        if argv and argv[0] in ("launch", "destroy", "packs"):
            ran.append(list(argv))
            return 0
        return real(argv)
    monkeypatch.setattr(cli, "main", fake_main)
    return ran


def test_bare_sentence_goes_to_the_translator_and_yes_runs_it(wired, capsys):
    assert cli.main(["Run and deploy rag-tutorial in databricks", "--yes"]) == 0
    assert wired == [["launch", "rag-sec-tutorial", "--name", "demo"]]
    assert "Interpreted as" in capsys.readouterr().err


def test_print_only_and_non_interactive_do_not_execute(wired, capsys):
    assert cli.main(["deploy rag-tutorial", "--print-only"]) == 0 and wired == []
    assert cli.main(["deploy rag-tutorial"]) == 2 and wired == []                      # no TTY and no --yes
    assert "--yes" in capsys.readouterr().err


def test_destroy_requires_typing_the_name(monkeypatch, wired):
    monkeypatch.setattr(nl, "call_llm", lambda *a, **k: json.dumps({"action": "destroy", "pack": "rag-tutorial", "name": None, "params": {}}))
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda s: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli.main(["delete rag-tutorial"]) == 1 and wired == []                      # 'y' is not the name
    monkeypatch.setattr("builtins.input", lambda prompt="": "demo")
    assert cli.main(["delete rag-tutorial"]) == 0 and wired == [["destroy", "rag-sec-tutorial", "--name", "demo"]]


def test_known_subcommands_still_work_and_aliases_resolve(capsys):
    assert cli.main(["packs"]) == 0
    assert cli.main(["launch", "rag-tutorial", "--dry-run", "--output", "-"]) == 0       # alias + default name, no --name needed
    assert '"cohort": "demo"' in capsys.readouterr().out


def test_unreachable_model_gives_an_actionable_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise nl.NLError("cannot reach the model endpoint at http://localhost:11434/v1. Run: ollama serve")
    monkeypatch.setattr(nl, "call_llm", boom)
    assert cli.main(["deploy rag-tutorial"]) == 2 and "ollama serve" in capsys.readouterr().err


def test_the_guess_guard_stays_correct_as_more_packs_exist():
    """Regression: adding a pack once made 'rag' look distinctive. A word shared by 3+ packs never backs a model's guess."""
    from labagent.nl import _distinctive_tokens, literal_pack
    tut = _distinctive_tokens(PACKS["rag-sec-tutorial"], PACKS)
    assert "rag" not in tut and {"tutorial", "notebooks"} <= tut
    assert {"agent", "databricks"} & _distinctive_tokens(PACKS["db-agent"], PACKS)
    assert literal_pack("deploy rag-tutorial on databricks", PACKS).id == "rag-sec-tutorial"      # naming the alias outright still wins
    assert literal_pack("launch rag-ide for me", PACKS).id == "rag-ide"
    assert literal_pack("start the rag-tutorial notebooks", PACKS).id == "rag-sec-tutorial"


def test_removed_pack_names_point_to_the_tutorial_instead_of_guessing():
    for old in ("rag-sec", "rag-sec-filings", "rag-lakebase", "rag-sec-filings-live"):
        with pytest.raises(KeyError, match="was removed.*rag-sec-tutorial"):
            registry.resolve(old)
