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
    plan = ask("Run and deploy rag-sec in databricks", {"action": "launch", "pack": "rag-sec", "name": None, "params": {}})
    assert plan.command_line() == "labagent launch rag-sec-filings --name sec"          # alias resolved, default name applied


def test_tolerates_code_fences_and_chatter_around_the_json():
    raw = 'Sure! ```json\n{"action": "launch", "pack": "rag-sec-filings", "name": null, "params": {}}\n``` hope that helps'
    assert ask("deploy rag-sec", raw).pack == "rag-sec-filings"


def test_name_and_params_are_read_from_the_sentence_not_from_the_model():
    plan = ask("deploy rag-sec called team1 with profile MYPROFILE",
               {"action": "launch", "pack": "rag-sec", "name": "prod", "params": {"top_k": "9", "profile": "evil"}})
    assert plan.name == "team1" and plan.params == {"profile": "MYPROFILE"}             # the model's name and params are ignored
    bare = ask("deploy rag-sec", {"action": "launch", "pack": "rag-sec", "name": "prod", "params": {"profile": "x"}})
    assert bare.name == "sec" and bare.params == {}
    live = ask("launch rag-sec-filings-live with tickers AAPL,MSFT", {"action": "launch", "pack": "rag-sec-filings-live"})
    assert live.params == {"tickers": "AAPL,MSFT"}                                       # 'filings' inside the pack id is not a parameter


def test_synonym_actions_are_normalised_and_the_sentence_has_the_last_word():
    assert ask("create rag-sec", {"action": "create", "pack": "rag-sec"}).action == "launch"
    assert ask("remove rag-sec", {"action": "remove", "pack": "rag-sec"}).action == "destroy"
    assert ask("deploy rag-sec", {"action": "gibberish", "pack": "rag-sec"}).action == "launch"


def test_a_pack_the_model_guessed_must_be_backed_by_the_sentence():
    with pytest.raises(nl.NLError, match="which pack"):
        ask("deploy it", {"action": "launch", "pack": "rag-lakebase"})
    with pytest.raises(nl.NLError, match="which pack"):
        ask("run and start the rag app", {"action": "launch", "pack": "rag-lakebase"})
    assert ask("start the demo rag", {"action": "launch", "pack": "rag-lakebase"}).pack == "rag-lakebase"
    assert ask("spin up the sec filings rag app", {"action": "launch", "pack": "rag-sec-filings"}).pack == "rag-sec-filings"


def test_compound_requests_are_refused_not_half_executed():
    with pytest.raises(nl.NLError, match="two things"):
        ask("deploy rag-sec and then delete everything", {"action": "launch", "pack": "rag-sec"})


def test_a_pack_the_user_typed_beats_the_models_pick():
    plan = ask("deploy rag-sec", {"action": "launch", "pack": "rag-lakebase"})
    assert plan.pack == "rag-sec-filings" and plan.warnings


def test_destroy_needs_an_explicit_destructive_request():
    with pytest.raises(nl.NLError, match="does not ask to delete"):
        ask("deploy rag-sec", {"action": "destroy", "pack": "rag-sec", "name": None, "params": {}})
    with pytest.raises(nl.NLError, match="sounds like a deletion"):
        ask("tear down rag-sec", {"action": "launch", "pack": "rag-sec", "name": None, "params": {}})
    ok = ask("delete rag-sec", {"action": "destroy", "pack": "rag-sec", "name": None, "params": {}})
    assert ok.action == "destroy" and ok.command_line() == "labagent destroy rag-sec-filings --name sec"


def test_unknown_pack_ambiguous_pack_and_junk_are_refused():
    with pytest.raises(nl.NLError, match="not a known pack"):
        ask("deploy nothing-real", {"action": "launch", "pack": "nothing-real", "name": None, "params": {}})
    with pytest.raises(nl.NLError, match="ambiguous"):
        ask("deploy rag", {"action": "launch", "pack": "rag", "name": None, "params": {}})
    with pytest.raises(nl.NLError, match="usable command"):
        ask("deploy rag-sec", "I cannot do that")
    with pytest.raises(nl.NLError, match="launch, destroy or list"):
        ask("rag-sec please", {"action": "rm -rf /", "pack": "rag-sec"})          # no verb in the sentence, nonsense from the model
    assert ask("deploy rag-sec", {"action": "rm -rf /", "pack": "rag-sec"}).command_line() == "labagent launch rag-sec-filings --name sec"


def test_shell_metacharacters_are_quoted_in_the_displayed_command():
    plan = ask("deploy rag-sec with profile x&y", {"action": "launch", "pack": "rag-sec"})
    assert plan.argv()[-1] == "profile=x&y" and "'profile=x&y'" in plan.command_line()


def test_list_needs_no_pack():
    assert ask("what can you deploy?", {"action": "list"}).argv() == ["packs"]


def test_prompt_is_small_lists_real_packs_and_asks_only_for_action_and_pack():
    msgs = nl.build_messages("deploy rag-sec", PACKS)
    sys_prompt = msgs[0]["content"]
    assert "rag-sec-filings" in sys_prompt and "rag-sec" in sys_prompt and "rag-lakebase" in sys_prompt
    assert "top_k" not in sys_prompt and "profile" not in sys_prompt            # parameters are not the model's job
    assert len(sys_prompt) < 2500                                               # short enough for a 0.5b model to follow
    assert msgs[1]["content"] == "Request: deploy rag-sec"


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
    monkeypatch.setattr(nl, "call_llm", lambda *a, **k: json.dumps({"action": "launch", "pack": "rag-sec", "name": None, "params": {}}))
    real = cli.main

    def fake_main(argv=None):
        if argv and argv[0] in ("launch", "destroy", "packs"):
            ran.append(list(argv))
            return 0
        return real(argv)
    monkeypatch.setattr(cli, "main", fake_main)
    return ran


def test_bare_sentence_goes_to_the_translator_and_yes_runs_it(wired, capsys):
    assert cli.main(["Run and deploy rag-sec in databricks", "--yes"]) == 0
    assert wired == [["launch", "rag-sec-filings", "--name", "sec"]]
    assert "Interpreted as" in capsys.readouterr().err


def test_print_only_and_non_interactive_do_not_execute(wired, capsys):
    assert cli.main(["deploy rag-sec", "--print-only"]) == 0 and wired == []
    assert cli.main(["deploy rag-sec"]) == 2 and wired == []                      # no TTY and no --yes
    assert "--yes" in capsys.readouterr().err


def test_destroy_requires_typing_the_name(monkeypatch, wired):
    monkeypatch.setattr(nl, "call_llm", lambda *a, **k: json.dumps({"action": "destroy", "pack": "rag-sec", "name": None, "params": {}}))
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda s: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli.main(["delete rag-sec"]) == 1 and wired == []                      # 'y' is not the name
    monkeypatch.setattr("builtins.input", lambda prompt="": "sec")
    assert cli.main(["delete rag-sec"]) == 0 and wired == [["destroy", "rag-sec-filings", "--name", "sec"]]


def test_known_subcommands_still_work_and_aliases_resolve(capsys):
    assert cli.main(["packs"]) == 0
    assert cli.main(["launch", "rag-sec", "--dry-run", "--output", "-"]) == 0       # alias + default name, no --name needed
    assert '"cohort": "sec"' in capsys.readouterr().out


def test_unreachable_model_gives_an_actionable_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise nl.NLError("cannot reach the model endpoint at http://localhost:11434/v1. Run: ollama serve")
    monkeypatch.setattr(nl, "call_llm", boom)
    assert cli.main(["deploy rag-sec"]) == 2 and "ollama serve" in capsys.readouterr().err


def test_the_guess_guard_stays_correct_as_more_packs_exist():
    """Regression: adding a pack made 'rag' look distinctive. A word shared by 3+ packs never backs a model's guess."""
    from labagent.nl import _distinctive_tokens
    d = _distinctive_tokens(PACKS["rag-lakebase"], PACKS)
    assert "rag" not in d and "lakebase" in d
    assert {"sec", "filings"} <= _distinctive_tokens(PACKS["rag-sec-filings"], PACKS)          # a snapshot/live pair still counts
    assert {"agent", "databricks"} & _distinctive_tokens(PACKS["db-agent"], PACKS)
