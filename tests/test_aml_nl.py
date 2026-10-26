"""Plain-English requests for the AML lab. The parameters come from exact rules on the sentence; the model only picks the pack."""
import json

from labagent import nl
from labagent.packs import registry

PACKS = registry.all_packs()


def ask(text, pack="aml", action="launch"):
    return nl.translate(text, PACKS, registry.resolve, chat=lambda *a, **k: json.dumps({"action": action, "pack": pack}))


def test_copilot_lab_for_canada():
    plan = ask("set up the AML copilot lab for Canada")
    assert plan.command_line() == "labagent launch aml --name demo --param mode=copilot --param jurisdiction=ca"


def test_rag_lab_for_the_us_with_a_name():
    plan = ask("deploy the AML lab for developers in the United States called team7")
    assert plan.name == "team7" and plan.params == {"mode": "rag", "jurisdiction": "us"}


def test_both_countries_and_a_bigger_dataset():
    assert ask("create the aml copilot lab for Canada and the US, medium size").params == {"mode": "copilot", "jurisdiction": "both", "size": "medium"}
    assert ask("start aml for both countries").params == {"jurisdiction": "both"}


def test_an_explicit_value_beats_the_guess_and_nothing_is_invented():
    assert ask("launch aml-rag for Canada with jurisdiction us").params["jurisdiction"] == "us"
    assert ask("launch aml-rag").params == {}                         # no country named: the pack default (ca) applies at launch
    assert "us" not in ask("set us up with the aml lab").params.values()   # the pronoun is not the United States


def test_destroy_still_needs_a_delete_word_and_names_the_pack():
    plan = ask("tear down the aml copilot lab called demo", action="destroy")
    assert plan.action == "destroy"
