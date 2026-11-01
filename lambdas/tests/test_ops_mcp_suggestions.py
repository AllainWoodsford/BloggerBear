"""The assistant's suggestion catalogue (ops_mcp/suggestions.py): fixed commands, ids checked,
nothing that deletes, and every command one that admin_cli really accepts."""

from __future__ import annotations

import importlib.util
import shlex
import sys
from pathlib import Path

import pytest

from ops_mcp import suggestions

ADMIN_CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "admin_cli.py"
ARTICLE_ID = "f5e88f3a-3c7e-48be-ae8b-52a31030ae5e"
# A kind either has a command or says what to look at; the two lists together are the catalogue.
WITH_A_COMMAND = sorted(kind for kind, entry in suggestions.CATALOGUE.items() if entry.arguments is not None)
WITHOUT_A_COMMAND = sorted(set(suggestions.CATALOGUE) - set(WITH_A_COMMAND))


@pytest.fixture(scope="module")
def admin_cli_parser():
    spec = importlib.util.spec_from_file_location("admin_cli_for_catalogue", ADMIN_CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    # admin_cli's build_parser imports its eval commands from a sibling module in scripts/ by name,
    # so scripts/ has to be on the path whichever directory pytest was started from.
    if str(ADMIN_CLI_PATH.parent) not in sys.path:
        sys.path.insert(0, str(ADMIN_CLI_PATH.parent))
    spec.loader.exec_module(module)
    return module.build_parser()


@pytest.mark.parametrize("kind", WITH_A_COMMAND)
def test_every_suggested_command_is_one_admin_cli_accepts(kind, admin_cli_parser):
    suggestion = suggestions.suggest(kind, ARTICLE_ID)

    assert suggestion["command"].startswith(suggestions.ADMIN_CLI + " ")
    arguments = shlex.split(suggestion["command"][len(suggestions.ADMIN_CLI) :])
    admin_cli_parser.parse_args(arguments)  # exits (SystemExit) on a command that does not exist
    assert suggestion["action"] and suggestion["what_it_does"]


@pytest.mark.parametrize("kind", WITH_A_COMMAND)
def test_nothing_in_the_catalogue_deletes_or_forces(kind):
    arguments = suggestions.CATALOGUE[kind].arguments.lower()

    for word in ("delete", "unpublish", "reject", "--force"):
        assert word not in arguments


@pytest.mark.parametrize("kind", WITHOUT_A_COMMAND)
def test_a_kind_with_no_command_says_what_to_look_at_and_nothing_to_run(kind):
    entry = suggestions.CATALOGUE[kind]

    assert entry.what_it_does is None  # there is no "it"
    assert "admin_cli" not in entry.action and "python" not in entry.action
    # Whatever the id is, even one that would be refused in a command: it goes nowhere.
    for target_id in (ARTICLE_ID, None, "a1; topics delete crypto"):
        assert suggestions.suggest(kind, target_id) == {
            "action": entry.action,
            "command": None,
            "what_it_does": None,
        }


def test_the_kinds_with_no_command_are_the_ones_the_cli_cannot_fix():
    # What log_review finds (one kind per root cause) is fixed in Terraform, code or the source,
    # never from admin_cli: log_review.py's CAUSES, each as log_<cause>.
    # And what api_errors finds in the access logs, the same way: api_<cause>.
    from ops_mcp import api_errors, log_review

    log_kinds = [f"log_{cause.key}" for cause in (*log_review.CAUSES, log_review.OTHER)]
    log_kinds += [f"api_{key}" for key in api_errors.CAUSES]
    # A security incident and a firewall spike have one each now: `security acknowledge` and
    # `security open`.
    # The vision cards (vision_status.py): a sites config is written by hand, and the worker is a
    # deployment setting.
    vision = [
        "vision_config_error",
        "vision_site_refused",
        "vision_site_starved",
        "vision_source_error",
        "vision_worker_error",
        "vision_worker_off",
    ]
    others = ["alarm_firing", "musing_dangling", "spend_unusual", *vision]
    assert WITHOUT_A_COMMAND == sorted(others + log_kinds)
    for kind in WITH_A_COMMAND:
        assert suggestions.CATALOGUE[kind].what_it_does


@pytest.mark.parametrize("kind", ["title_markup", "body_code_fence", "title_markup_and_body_code_fence"])
def test_a_published_article_that_looks_wrong_is_rewritten_with_fixed_words(kind, admin_cli_parser):
    command = suggestions.suggest(kind, ARTICLE_ID)["command"]

    parsed = admin_cli_parser.parse_args(shlex.split(command[len(suggestions.ADMIN_CLI) :]))
    words = shlex.split(suggestions.CATALOGUE[kind].arguments)[-1]
    assert vars(parsed)["instructions"] == words and "{" not in words
    assert suggestions.suggest(kind, "a1; topics delete crypto") is None


def test_a_blank_musing_is_written_again_where_it_is_and_the_article_is_left_alone(admin_cli_parser):
    command = suggestions.suggest("musing_no_text", ARTICLE_ID)["command"]

    assert command == f"python scripts/admin_cli.py musings regenerate --article {ARTICLE_ID}"
    parsed = vars(admin_cli_parser.parse_args(shlex.split(command[len(suggestions.ADMIN_CLI) :])))
    # The id is the article's (what content_checks reports), never taken for a musing's.
    assert parsed["article_id"] == ARTICLE_ID and parsed["musing_id"] is None
    assert "rewrite" not in command
    assert suggestions.suggest("musing_no_text", "a1; topics delete crypto") is None


def test_the_id_is_put_into_the_command_as_it_is():
    command = suggestions.suggest("draft_truncated", ARTICLE_ID)["command"]

    assert (
        command == f'python scripts/admin_cli.py articles rewrite {ARTICLE_ID} -i "the draft was cut short"'
    )


@pytest.mark.parametrize(
    "hostile",
    [
        "a1; topics delete crypto",
        'a1" --force',
        "a1 && rm -rf .",
        "$(whoami)",
        "a1\ntopics delete crypto",
        "-rf",
        "",
        "x" * 200,
        None,
        42,
    ],
)
def test_an_id_that_is_not_a_plain_id_gets_no_command(hostile):
    assert suggestions.suggest("draft_truncated", hostile) is None
    assert suggestions.suggest("research_overdue", hostile) is None


def test_a_command_that_takes_no_id_needs_none():
    assert suggestions.suggest("awaiting_review")["command"].endswith("approve --source moderation")
    assert suggestions.suggest("awaiting_review", "not an id!")["command"].endswith(
        "approve --source moderation"
    )


def test_an_unknown_kind_suggests_nothing():
    assert suggestions.suggest("topics_delete", "crypto") is None


def test_a_finding_carries_its_suggestion_and_only_the_where_it_was_given():
    found = suggestions.finding(
        "research_overdue", "Crypto wasn't researched on time", "crypto", topic="Crypto", article_id=None
    )

    assert found["kind"] == "research_overdue" and found["id"] == "crypto"
    assert found["where"] == {"topic": "Crypto"}
    assert found["suggestion"]["command"].endswith("topics trigger crypto --pipeline research_tick")


def test_a_finding_with_an_untrusted_id_is_still_reported_without_a_command():
    found = suggestions.finding("draft_truncated", "A draft was cut short", "a1; topics delete crypto")

    assert found["noticed"] == "A draft was cut short" and found["suggestion"] is None


# The Model Ranch's cards (model_ranch.py): a model's id and a prompt's are not shaped as an article's.
MODEL_ID = "maker.alpha-v1:0"
PROFILE_ARN = "arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/maker.alpha-v1:0"


@pytest.mark.parametrize("model_id", [MODEL_ID, PROFILE_ARN])
def test_a_gated_model_gets_the_ungate_command_with_its_model_id(model_id, admin_cli_parser):
    command = suggestions.suggest("model_gated", model_id)["command"]

    assert command == f"python scripts/admin_cli.py evals ungate --model {model_id} --hours 48"
    parsed = vars(admin_cli_parser.parse_args(shlex.split(command[len(suggestions.ADMIN_CLI) :])))
    assert (parsed["model"], parsed["hours"]) == (model_id, 48)


@pytest.mark.parametrize(
    "hostile",
    ["maker.alpha-v1:0; evals cull --apply --yes", "maker alpha", '"maker"', "$(whoami)", "-rf",
     "maker.alpha-v1:0\n", "maker#alpha", "", "m" * 161, None],
)  # fmt: skip
def test_a_model_id_that_is_not_one_gets_no_command(hostile):
    assert suggestions.suggest("model_gated", hostile) is None
    assert suggestions.valid_id("model_gated", hostile) is False


def test_each_kind_checks_its_own_ids_shape():
    assert suggestions.valid_id("prompt_fallback", "daily_cycle/draft")
    assert not suggestions.valid_id("prompt_fallback", "daily cycle/draft")
    assert not suggestions.valid_id("prompt_fallback", MODEL_ID)
    # An article's kinds keep the narrower pattern: no dots, colons or slashes.
    assert suggestions.valid_id("draft_truncated", ARTICLE_ID)
    assert not suggestions.valid_id("draft_truncated", MODEL_ID)
    assert not suggestions.valid_id("draft_truncated", f"{ARTICLE_ID}\n")


def test_the_ranch_cards_with_no_id_need_none():
    assert suggestions.suggest("judge_uncalibrated")["command"].endswith("approve --source findings")
    assert suggestions.suggest("prompt_fallback", "not a prompt id!")["command"].endswith("prompts list")
    assert suggestions.suggest("cull_proposed")["command"].endswith("evals cull --apply")


@pytest.mark.parametrize(("kind", "variant"), sorted(suggestions.VARIANTS))
def test_every_variant_is_a_command_admin_cli_accepts_that_deletes_nothing(kind, variant, admin_cli_parser):
    assert kind in suggestions.CATALOGUE
    suggestion = suggestions.suggest(kind, None, variant)

    admin_cli_parser.parse_args(shlex.split(suggestion["command"][len(suggestions.ADMIN_CLI) :]))
    assert suggestion["action"] and suggestion["what_it_does"] and suggestion["warning"]
    for word in ("delete", "unpublish", "reject", "--force"):
        assert word not in suggestions.VARIANTS[(kind, variant)].arguments


def test_while_culling_is_off_the_cull_card_names_turning_it_on_before_the_apply():
    off = suggestions.suggest("cull_proposed", variant=suggestions.CULL_OFF)
    on = suggestions.suggest("cull_proposed")

    assert off["command"].endswith("evals set --cull on")
    assert off["action"].index("evals set --cull on") < off["action"].index("evals cull --apply")
    assert on["command"].endswith("evals cull --apply")
    # A variant of a kind that has none, or of no kind at all, changes nothing.
    assert suggestions.suggest("model_gated", MODEL_ID, suggestions.CULL_OFF) == suggestions.suggest(
        "model_gated", MODEL_ID
    )
    assert suggestions.suggest("no_such_kind", variant=suggestions.CULL_OFF) is None
