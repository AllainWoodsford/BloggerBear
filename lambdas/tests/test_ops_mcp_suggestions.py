"""The assistant's suggestion catalogue (ops_mcp/suggestions.py): fixed commands, ids checked,
nothing that deletes, and every command one that admin_cli really accepts."""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path

import pytest

from ops_mcp import suggestions

ADMIN_CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "admin_cli.py"
ARTICLE_ID = "f5e88f3a-3c7e-48be-ae8b-52a31030ae5e"


@pytest.fixture(scope="module")
def admin_cli_parser():
    spec = importlib.util.spec_from_file_location("admin_cli_for_catalogue", ADMIN_CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_parser()


@pytest.mark.parametrize("kind", sorted(suggestions.CATALOGUE))
def test_every_suggested_command_is_one_admin_cli_accepts(kind, admin_cli_parser):
    suggestion = suggestions.suggest(kind, ARTICLE_ID)

    assert suggestion["command"].startswith(suggestions.ADMIN_CLI + " ")
    arguments = shlex.split(suggestion["command"][len(suggestions.ADMIN_CLI) :])
    admin_cli_parser.parse_args(arguments)  # exits (SystemExit) on a command that does not exist
    assert suggestion["action"] and suggestion["what_it_does"]


@pytest.mark.parametrize("kind", sorted(suggestions.CATALOGUE))
def test_nothing_in_the_catalogue_deletes_or_forces(kind):
    arguments = suggestions.CATALOGUE[kind].arguments.lower()

    for word in ("delete", "unpublish", "reject", "--force"):
        assert word not in arguments


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
