"""The daily cycle's drafting prompts as registry specs (common/prompts_registry.py; section 4.10
and "PR6–PR10. Migrate prompts" in docs/enhancements/model-ranch-prompt-assets-evals.md): each
v1.txt in prompts/ is the vanilla it was migrated from; with assets off, every prompt `_ideate`,
`_draft_article` and `_draft_title` send is byte for byte the text the inline f-strings built
before the migration, across the inputs that change it; the largest realistic render fits each
spec's max_chars; an approved asset is rendered with the call site's values and locked blocks and
its version reaches the lineage, and one that leaves out any value is refused for the vanilla; and
the handler binds the run's id for the A/B draw.

The `_legacy_*` builders below are the call sites' code as it was before the move, frozen here with
the v1 files' hashes as the migration's proof. They are not a standing byte gate (section 2): a
later code PR that edits a vanilla on purpose bumps its version and edits its builder with it, and
retiring a v1.txt to archive/ (section 4.2) breaks nothing here."""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import daily_cycle_handler as handler
from common import compliance, dynamo, equipment, evidence, fresh_review, prompts, prompts_registry
from common.adapters.base import DaySubject
from common.costing import build_lineage
from common.editorial_goals import EditorialGoal
from common.editorial_resolver import (
    DRAFT_ALIGNMENT_DIRECTIVE,
    MANDATE_ALIGNMENT_RULE,
    MAX_GOAL_TEXT_CHARS,
    resolve_editorial_goals,
)
from common.goal_rotation import Pick, RotatingGoal
from common.relevance import draft_relevance_boundary, ideation_relevance_rule, topic_label

ROOT = Path(__file__).resolve().parents[2]
DAILY_CYCLE_IDS = (
    "daily_cycle/ideation",
    "daily_cycle/ideation_goal",
    "daily_cycle/draft",
    "daily_cycle/title",
)
# The v1 vanillas as this migration copied them into prompts/, frozen here rather than read from
# the registry: a published v1 never changes, while a later code PR may edit a vanilla past v1.
V1_SHA256 = {
    "daily_cycle/ideation": "28ca57593ed076a474d2e1666e4761c53448dbbd221389e88872cd443c628d16",
    "daily_cycle/ideation_goal": "3a259a87474585a814cdc830708ad49b043224e18c35466325f86e9a21eb308c",
    "daily_cycle/draft": "bb1289e0db7f66e874f147289ca4fec18417a412d4c39827fc413846bd97b918",
    "daily_cycle/title": "62c0a80cc539e27eeb778104593d142aa62ae70db30f9d099e7f4273a321f1f3",
}


def _values_in(template: str) -> set[str]:
    """The `{{name}}` values a template places (its locked blocks left out)."""
    return {match.group(2) for match in prompts.PLACEHOLDER.finditer(template) if match.group(1) is None}


@pytest.fixture(autouse=True)
def _assets_off(monkeypatch):
    """PROMPT_AS_ASSET_ENABLED unset, as on every deployment until the owner turns it on."""
    for name in ("PROMPT_AS_ASSET_ENABLED", "PROMPTS_BUCKET", "PROMPT_HASHES_TABLE"):
        monkeypatch.delenv(name, raising=False)
    prompts._reset()
    yield
    prompts._reset()


def _tracked(text, model_id="m", used_fallback=False):
    return {
        "text": text,
        "model_id": model_id,
        "input_tokens": 10,
        "output_tokens": 5,
        "used_fallback": used_fallback,
        "stop_reason": "end_turn",
        "attempts": 1,
    }


def _sent(replies):
    """A patched invoke_model_tracked answering `replies` in turn (the last one again once they run
    out), and the list of the prompts it was sent."""
    sent = []

    def invoke(prompt, *args, **kwargs):
        sent.append(prompt)
        return _tracked(replies[min(len(sent), len(replies)) - 1])

    return patch("daily_cycle_handler.invoke_model_tracked", side_effect=invoke), sent


# --- the frozen pre-migration builders -------------------------------------------------------------


def _legacy_ideation(topic, summaries_block, guidance, goal, recent_titles, rotation):
    topic_name = topic_label(topic)
    goals = resolve_editorial_goals(topic, goal, handler._rotation_focus(rotation))
    standing_goal = (
        f"ACTIVE EDITORIAL MANDATE:\n{goals}\n\n"
        f"{ideation_relevance_rule(topic_name)}\n"
        f"{MANDATE_ALIGNMENT_RULE}"
        f"{handler._revisit_note(rotation)}"
    )
    already_covered = ""
    if recent_titles:
        titles_block = "\n".join(f"- {title}" for title in recent_titles)
        already_covered = (
            "\n\nAlready covered recently -- propose a genuinely different angle, not a rehash "
            f"of the same story with new numbers:\n{titles_block}"
        )
    if goal is None:
        prompt = (
            f"Based on the following recent research findings about "
            f"'{topic_name}', propose exactly "
            f"{handler._NUM_CANDIDATE_ANGLES} distinct, specific candidate article angles. "
            "Reply with exactly one angle per line, no numbering, no extra "
            "commentary.\n\n"
            f"{standing_goal}{already_covered}\n\n"
            f"Findings:\n{summaries_block}"
        )
    else:
        prompt = (
            "Based on the following recent research findings and the assigned daily "
            f"editorial vector for '{topic_name}', propose exactly "
            f"{handler._NUM_CANDIDATE_ANGLES} distinct, specific candidate article angles. "
            "Reply with exactly one angle per line, no numbering, no extra "
            "commentary.\n\n"
            f"{standing_goal}{already_covered}\n\n"
            f"Editorial Mandate: {handler._goal_mandate(goal)}\n"
            f"Apply this mandate strictly within the theme of '{topic_name}' and the Active "
            "Editorial Mandate above: it decides the shape of each angle, never the subject.\n\n"
            f"Data Payload:\n{summaries_block}"
        )
    if guidance:
        prompt += f"\n\nAdditional guidance based on reader feedback:\n{guidance}"
    if compliance.is_financial_topic(topic):
        prompt += f"\n\nFinancial-topic guidance (mandatory):\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"
    return prompt


def _legacy_draft(topic, angle, summaries_block, guidance, few_shot_excerpt, goal, rotation):
    topic_name = topic_label(topic)
    goals = resolve_editorial_goals(topic, goal, handler._rotation_focus(rotation))
    prompt = (
        "Write a full article draft in markdown (a few paragraphs) for a blog "
        f"about '{topic_name}', on this angle: {angle}\n\n"
        f"CORE EDITORIAL DIRECTION:\n{goals}"
        f"{handler._revisit_note(rotation)}\n\n"
        f"Base it on these recent findings:\n{summaries_block}"
        f"\n\n{draft_relevance_boundary(topic_name)}"
        f"\n\n{DRAFT_ALIGNMENT_DIRECTIVE}"
    )
    if goal is not None:
        prompt += f"\n\nArticle style: {handler._goal_style(goal)}"
    if guidance:
        prompt += f"\n\nAdditional guidance based on reader feedback:\n{guidance}"
    if compliance.is_financial_topic(topic):
        prompt += f"\n\nFinancial-topic guidance (mandatory):\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"
    rules = fresh_review.writing_rules(topic)
    source_guidance = "\n".join(filter(None, [rules["figure_guidance"], rules["drafting_guidance"]]))
    if source_guidance:
        prompt += f"\n\nHow articles on this topic are written (mandatory):\n{source_guidance}"
    if few_shot_excerpt:
        prompt += (
            "\n\nHere is an excerpt from a well-received past article on this "
            "topic, for style reference only (do not repeat its content):\n"
            f"{few_shot_excerpt}"
        )
    return prompt


def _legacy_title(angle, retry):
    prompt = (
        "Write a short, engaging article title for an article with this angle: "
        f"{angle}\n\n"
        f"Reply with the title only, as a single line of no more than {handler._MAX_TITLE_WORDS} words. "
        "No surrounding quotes, no markdown, no explanation, and no questions back -- if the "
        "angle is unclear, title it as best you can rather than asking for clarification."
    )
    if retry:
        prompt = (
            f"{prompt}\n\nYour previous reply was not a usable title -- reply with nothing but "
            "the title itself this time."
        )
    return prompt


# --- the inputs that change the text ---------------------------------------------------------------

# Values that look like placeholders, or like regex replacement escapes, must come through as typed:
# the old f-strings never read them, and rendering is one pass that never re-scans a value.
TRICKY = r"{{topic_name}} {{locked:financial_guidance}} \1 \g<0> $& {x}"
TOPICS = (
    {"topic_id": "github-trending", "name": "GitHub Trending", "adapter": "github_trending"},
    {"topic_id": "crypto", "name": "Crypto Markets", "adapter": "crypto_feed", "is_financial": True},
    {"topic_id": "rail", "name": f"Rail {TRICKY}", "adapter": "rail_access"},
    {
        "topic_id": "no-name-topic",
        "adapter": "not-an-adapter",
        "editorial_goals": {"primary_focus": "Lifts and ramps", "exclusion_criteria": "No timetables"},
    },
)
GOALS = (
    None,
    EditorialGoal.MARKET_NEWS,
    DaySubject(key="sydney", label="Sydney", mandate=f"Sydney's stations {TRICKY}", style="Plain and short."),
)
ROTATIONS = (
    None,
    Pick(goal=RotatingGoal("lifts", "Lifts out of service"), findings=[], state={}, revisit=False),
    Pick(goal=RotatingGoal("ramps", "Ramps at platforms"), findings=[], state={}, revisit=True),
)
GUIDANCE = (None, f"- Keep it short.\n- Name the repository. {TRICKY}")
SUMMARIES = f"- Repo X jumped to #1.\n- A finding that says {TRICKY}"


def test_each_daily_cycle_v1_asset_file_is_the_vanilla_it_was_migrated_from():
    """v1.txt sits in latest/ or, once retired, in archive/ (section 4.2), and its bytes never change.
    While a vanilla is still v1 the file is that vanilla byte for byte, so editing a vanilla without
    bumping its version fails here; a vanilla edited past v1 is no longer held to it."""
    specs = {spec.prompt_id: spec for spec in prompts_registry.all_specs()}
    for prompt_id in DAILY_CYCLE_IDS:
        folder = ROOT / "prompts" / prompt_id
        candidates = (folder / "latest" / "v1.txt", folder / "archive" / "v1.txt")
        found = [path for path in candidates if path.is_file()]
        assert found, f"{prompt_id}: v1.txt is in neither latest/ nor archive/"
        raw = found[0].read_bytes()
        assert hashlib.sha256(raw).hexdigest() == V1_SHA256[prompt_id], prompt_id
        if specs[prompt_id].vanilla_version == "v1":
            assert raw == specs[prompt_id].vanilla.encode("utf-8"), prompt_id


def test_the_prompt_assets_are_kept_lf_on_every_checkout():
    """The check above reads the files' raw bytes, so git must not give them CRLF on a Windows
    checkout, nor keep a CRLF an editor saved into one."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "prompts/** text eol=lf" in attributes


def test_the_daily_cycle_prompts_are_registered_as_assets_at_v1_with_their_locked_tails():
    specs = {spec.prompt_id: spec for spec in prompts_registry.all_specs()}
    assert {pid for pid in specs if pid.startswith("daily_cycle/")} == set(DAILY_CYCLE_IDS)
    for prompt_id in DAILY_CYCLE_IDS:
        assert specs[prompt_id].asset is True and specs[prompt_id].vanilla_version == "v1"
    assert specs["daily_cycle/ideation"].locked == ("financial_guidance",)
    assert specs["daily_cycle/ideation_goal"].locked == ("financial_guidance",)
    assert specs["daily_cycle/draft"].locked == ("financial_guidance", "source_guidance", "few_shot")
    assert specs["daily_cycle/title"].locked == ("reply_rule", "title_retry")
    # The rules owned elsewhere and the reply formats are values an asset must place, never reword.
    for prompt_id in ("daily_cycle/ideation", "daily_cycle/ideation_goal"):
        assert {"reply_rule", "relevance_rule", "alignment_rule", "summaries"} <= specs[prompt_id].required
    assert {"relevance_boundary", "alignment_directive", "summaries"} <= specs["daily_cycle/draft"].required
    # Every value a vanilla places is required and none is optional, so no asset can leave out the
    # gear's guidance, the recent titles, the revisit note, the day's mandate, the style or the angle.
    for prompt_id in DAILY_CYCLE_IDS:
        spec = specs[prompt_id]
        assert spec.optional == frozenset() and spec.required == _values_in(spec.vanilla), prompt_id


def test_with_assets_off_ideation_sends_exactly_the_pre_migration_text_for_every_input():
    cases = itertools.product(TOPICS, GOALS, ROTATIONS, GUIDANCE, (None, ["Old title", f"Another {TRICKY}"]))
    for topic, goal, rotation, guidance, titles in cases:
        invoke, sent = _sent(["Angle one\nAngle two\nAngle three"])
        with invoke:
            _, call = handler._ideate(
                topic,
                SUMMARIES,
                "m",
                None,
                guidance=guidance,
                goal=goal,
                recent_titles=titles,
                rotation=rotation,
            )
        expected = _legacy_ideation(topic, SUMMARIES, guidance, goal, titles, rotation)
        assert sent == [expected], (topic["topic_id"], goal, rotation, guidance, titles)
        assert call["prompt_id"] == ("daily_cycle/ideation" if goal is None else "daily_cycle/ideation_goal")
        assert call["prompt_version"] == "vanilla:v1"


def test_with_assets_off_the_fallback_model_retry_of_ideation_sends_the_same_text():
    topic = TOPICS[1]
    sent = []

    def invoke(prompt, model_id, **kwargs):
        sent.append((prompt, model_id))
        if model_id == "m":
            return {**_tracked(""), "stop_reason": "max_tokens"}  # all thinking, no text
        return _tracked("Angle one", model_id=model_id)

    with patch("daily_cycle_handler.invoke_model_tracked", side_effect=invoke):
        handler._ideate(topic, SUMMARIES, "m", "fallback-m")

    expected = _legacy_ideation(topic, SUMMARIES, None, None, None, None)
    assert sent == [(expected, "m"), (expected, "fallback-m")]


def test_with_assets_off_the_draft_sends_exactly_the_pre_migration_text_for_every_input():
    angle = f"An angle about {TRICKY}"
    cases = itertools.product(TOPICS, GOALS, ROTATIONS, GUIDANCE, (None, f"Excerpt {TRICKY}"))
    for topic, goal, rotation, guidance, few_shot in cases:
        invoke, sent = _sent(["# Draft"])
        with invoke:
            _, call = handler._draft_article(
                topic,
                angle,
                SUMMARIES,
                "m",
                None,
                guidance=guidance,
                few_shot_excerpt=few_shot,
                goal=goal,
                rotation=rotation,
            )
        expected = _legacy_draft(topic, angle, SUMMARIES, guidance, few_shot, goal, rotation)
        assert sent == [expected], (topic["topic_id"], goal, rotation, guidance, few_shot)
        assert call["prompt_id"] == "daily_cycle/draft" and call["prompt_version"] == "vanilla:v1"


@pytest.mark.parametrize("angle", ["A plain angle", f"An angle with {TRICKY}", ""])
def test_with_assets_off_the_title_and_its_retry_send_exactly_the_pre_migration_text(angle):
    invoke, sent = _sent(["A Good Title"])
    with invoke:
        handler._draft_title(angle, "m", None)
    assert sent == [_legacy_title(angle, retry=False)]

    invoke, sent = _sent(["I cannot title this without more detail.", "A Good Title"])
    with invoke:
        _, call, plausible = handler._draft_title(angle, "m", None)
    assert sent == [_legacy_title(angle, retry=False), _legacy_title(angle, retry=True)]
    assert plausible and call["attempts"] == 2
    assert call["prompt_id"] == "daily_cycle/title" and call["prompt_version"] == "vanilla:v1"


def test_with_assets_off_no_prompt_fallback_line_is_written(capsys):
    invoke, _ = _sent(["Angle one", "# Draft", "A Title"])
    with invoke:
        handler._ideate(TOPICS[0], SUMMARIES, "m", None)
        handler._draft_article(TOPICS[0], "An angle", SUMMARIES, "m", None)
        handler._draft_title("An angle", "m", None)
    assert evidence.PREFIX not in capsys.readouterr().out


def test_the_largest_realistic_render_of_each_prompt_fits_its_max_chars():
    """Every value at the limit the code or the Admin API holds it to: the findings block at
    SUMMARIES_MAX_CHARS, the gear at MAX_GUIDANCE_CHARS, the editorial goal fields, a revisit
    rotation's primary focus (which stands in for the topic's own) and a day subject's mandate and
    style at MAX_GOAL_TEXT_CHARS, RECENT_TITLES_LIMIT long titles, a full few-shot excerpt, a
    financial topic with its adapter's writing rules and a long name."""
    long = "x" * MAX_GOAL_TEXT_CHARS
    topic = {
        "topic_id": "crypto",
        "name": "N" * 100,
        "adapter": "crypto_feed",
        "is_financial": True,
        "editorial_goals": {"primary_focus": long, "exclusion_criteria": long},
    }
    goal = DaySubject(key="sydney", label="Sydney", mandate=long, style=long)
    summaries = "- " + "s" * (handler.SUMMARIES_MAX_CHARS - 2)
    guidance = "g" * equipment.MAX_GUIDANCE_CHARS
    titles = ["t" * 200] * handler.RECENT_TITLES_LIMIT
    revisit = Pick(goal=RotatingGoal("ramps", long), findings=[], state={}, revisit=True)
    angle = " ".join(["a" * 20] * 40)
    specs = {spec.prompt_id: spec for spec in prompts_registry.all_specs()}

    # The title's longest form is its retry, so the first title reply is not a usable title.
    invoke, sent = _sent(["Angle one", "Angle one", "# Draft", "I cannot.", "A Title"])
    with invoke:
        ideation = {"guidance": guidance, "recent_titles": titles, "rotation": revisit}
        handler._ideate(topic, summaries, "m", None, goal=goal, **ideation)
        handler._ideate(topic, summaries, "m", None, **ideation)
        handler._draft_article(
            topic,
            angle,
            summaries,
            "m",
            None,
            guidance=guidance,
            few_shot_excerpt="f" * handler._FEW_SHOT_EXCERPT_CHARS,
            goal=goal,
            rotation=revisit,
        )
        handler._draft_title(angle, "m", None)

    for prompt_id, text in zip(
        ("daily_cycle/ideation_goal", "daily_cycle/ideation", "daily_cycle/draft", "daily_cycle/title"),
        [sent[0], sent[1], sent[2], sent[4]],
        strict=True,
    ):
        assert len(text) < specs[prompt_id].max_chars * 0.9, (prompt_id, len(text))


def test_the_prompt_fields_survive_into_the_articles_lineage():
    invoke, _ = _sent(["# Draft"])
    with invoke:
        _, call = handler._draft_article(TOPICS[0], "An angle", SUMMARIES, "m", None)
    with patch("common.costing.pricing_for", return_value=None):  # unpriced: no models table here
        lineage = build_lineage([call])
    assert lineage["calls"][0]["prompt_id"] == "daily_cycle/draft"
    assert lineage["calls"][0]["prompt_version"] == "vanilla:v1"


# --- the run boundary ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event,context,expected",
    [
        ({"execution": {"id": "arn:aws:states:exec-1"}}, None, "arn:aws:states:exec-1"),
        ({}, SimpleNamespace(aws_request_id="req-1"), "req-1"),
        ({}, None, None),
    ],
)
def test_the_daily_run_binds_its_run_id_for_the_draw_and_lets_go_after(event, context, expected):
    seen = []

    def run(topic_id, force=False, run_id=None):
        seen.append((prompts.bound_run(), run_id))
        return {"status": "no_findings", "topic_id": topic_id}

    with prompts.bind_run("outer"), patch("daily_cycle_handler._run_daily_cycle", side_effect=run):
        handler._handle({"topic_id": "github-trending", **event}, context)
        assert prompts.bound_run() == "outer"
    assert seen == [(expected, expected)]


def test_a_run_that_raises_still_lets_go_of_its_run_id():
    with patch("daily_cycle_handler._run_daily_cycle", side_effect=RuntimeError("boom")):
        result = handler._handle({"topic_id": "t", "execution": {"id": "exec-9"}}, None)
    assert result["status"] == "error" and prompts.bound_run() is None


# --- with an approved asset ------------------------------------------------------------------------

REGION = "ap-southeast-2"


@pytest.fixture
def assets(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "PROMPT_AS_ASSET_ENABLED": "true",
        "PROMPTS_BUCKET": "prompts-test",
        "PROMPT_HASHES_TABLE": "PromptHashes",
        "MODEL_CONFIG_TABLE": "ModelConfig",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    prompts._reset()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="prompts-test", CreateBucketConfiguration={"LocationConstraint": REGION})
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name, key in {"PromptHashes": "prompt_hash", "ModelConfig": "config_id"}.items():
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        hashes = boto3.resource("dynamodb", region_name=REGION).Table("PromptHashes")

        def publish(prompt_id, version, text):
            sha = prompts.sha256_text(text)
            s3.put_object(
                Bucket="prompts-test",
                Key=f"{prompt_id}/latest/{version}.txt",
                Body=text.encode("utf-8"),
                Tagging=f"version={version}&sha256={sha}",
            )
            row = {"prompt_hash": sha, "prompt_id": prompt_id, "version": version, "status": "live"}
            hashes.put_item(Item=row)

        yield publish
    dynamo._dynamodb_resource = None
    prompts._reset()


def test_an_approved_title_asset_is_sent_with_the_locked_reply_rule_and_its_version_is_in_the_lineage(assets):
    title = "Title this, plainly: {{angle}}\n{{locked:reply_rule}}{{locked:title_retry}}"
    assets("daily_cycle/title", "v2", title)
    invoke, sent = _sent(["I cannot title this.", "A Good Title"])
    with invoke:
        _, call, _ = handler._draft_title("Lifts at Central", "m", None)

    first = f"Title this, plainly: Lifts at Central\n{handler._TITLE_REPLY_RULE}"
    assert sent == [first, first + handler._TITLE_RETRY]
    assert call["prompt_id"] == "daily_cycle/title" and call["prompt_version"] == "v2"


def test_a_split_title_asset_gives_the_retry_the_same_arm_as_the_first_call_in_a_bound_run(assets):
    assets("daily_cycle/title", "v2", "Arm two: {{angle}}\n{{locked:reply_rule}}{{locked:title_retry}}")
    assets("daily_cycle/title", "v3", "Arm three: {{angle}}\n{{locked:reply_rule}}{{locked:title_retry}}")
    for run_key in ("exec-1", "exec-2", "exec-3", "exec-4"):
        invoke, sent = _sent(["I cannot title this.", "A Good Title"])
        with invoke, prompts.bind_run(run_key):
            _, call, _ = handler._draft_title("An angle", "m", None)
        arm = "Arm two" if call["prompt_version"] == "v2" else "Arm three"
        assert sent[0].startswith(arm) and sent[1].startswith(arm), run_key


def test_the_title_lineage_records_the_retrys_version_when_only_the_retry_falls_back(assets, capsys):
    """An asset whose first render fits max_chars but whose retry, one line longer, does not: the
    retry is the vanilla's, and the lineage records the version of that call, the one that counted."""
    spec = prompts.spec_for("daily_cycle/title")
    angle = "Lifts at Central"
    pad = "p" * (spec.max_chars - 50 - len(f"{angle}\n{handler._TITLE_REPLY_RULE}"))
    assets("daily_cycle/title", "v2", pad + "{{angle}}\n{{locked:reply_rule}}{{locked:title_retry}}")
    invoke, sent = _sent(["I cannot title this.", "A Good Title"])
    with invoke:
        _, call, _ = handler._draft_title(angle, "m", None)

    first = f"{pad}{angle}\n{handler._TITLE_REPLY_RULE}"
    # The premise, so the test cannot quietly become a same-version case if a limit or a rule changes.
    assert len(first) <= spec.max_chars < len(first + handler._TITLE_RETRY)
    assert sent == [first, _legacy_title(angle, retry=True)]
    assert call["prompt_id"] == "daily_cycle/title" and call["prompt_version"] == "vanilla:v1"
    out = capsys.readouterr().out.splitlines()
    lines = [evidence.parse(line) for line in out if line.startswith(evidence.PREFIX)]
    fallbacks = [(line["code"], line["version"]) for line in lines if line["event"] == "prompt_fallback"]
    assert fallbacks == [("too_large", "v2")]


def test_an_approved_draft_asset_keeps_the_financial_and_adapter_rules_at_its_end(assets):
    assets(
        "daily_cycle/draft",
        "v2",
        "Findings first:\n{{summaries}}\n\nNow write for '{{topic_name}}' on: {{angle}}\n"
        "{{editorial_goals}}{{revisit_note}}\n{{relevance_boundary}}\n{{alignment_directive}}"
        "{{style_block}}{{guidance_block}}"
        "{{locked:financial_guidance}}{{locked:source_guidance}}{{locked:few_shot}}",
    )
    topic = TOPICS[1]
    invoke, sent = _sent(["# Draft"])
    with invoke:
        _, call = handler._draft_article(topic, "An angle", SUMMARIES, "m", None, few_shot_excerpt="Past.")

    assert call["prompt_version"] == "v2"
    head = f"Findings first:\n{SUMMARIES}\n\nNow write for 'Crypto Markets' on: An angle\n"
    assert sent[0].startswith(head)
    legacy = _legacy_draft(topic, "An angle", SUMMARIES, None, "Past.", None, None)
    tail = legacy[legacy.index("\n\nFinancial-topic guidance (mandatory):") :]
    assert sent[0].endswith(tail)


def test_a_draft_asset_that_leaves_out_the_findings_is_refused_for_the_exact_vanilla(assets, capsys):
    assets(
        "daily_cycle/draft",
        "v2",
        "Write for '{{topic_name}}' on: {{angle}} {{editorial_goals}}{{revisit_note}} {{relevance_boundary}} "
        "{{alignment_directive}}{{style_block}}{{guidance_block}}"
        "{{locked:financial_guidance}}{{locked:source_guidance}}{{locked:few_shot}}",
    )
    invoke, sent = _sent(["# Draft"])
    with invoke:
        _, call = handler._draft_article(TOPICS[0], "An angle", SUMMARIES, "m", None)

    assert sent == [_legacy_draft(TOPICS[0], "An angle", SUMMARIES, None, None, None, None)]
    assert call["prompt_version"] == "vanilla:v1"
    out = capsys.readouterr().out.splitlines()
    lines = [evidence.parse(line) for line in out if line.startswith(evidence.PREFIX)]
    assert [line["code"] for line in lines if line["event"] == "prompt_fallback"] == ["missing_placeholder"]


# Every value each vanilla places, read from the vanilla itself rather than from the spec's
# `required`, which would shrink along with a value moved to `optional`.
DROPS = [
    (spec.prompt_id, name)
    for spec in prompts_registry.all_specs()
    if spec.prompt_id in DAILY_CYCLE_IDS
    for name in sorted(_values_in(spec.vanilla))
]


@pytest.mark.parametrize("prompt_id,name", DROPS)
def test_an_asset_that_leaves_out_any_value_is_refused_for_the_exact_vanilla(assets, capsys, prompt_id, name):
    """The inputs make every value non-empty (a financial topic, a day subject, a revisit, the gear's
    guidance, recent titles), so an asset served without one would send different text."""
    assets(prompt_id, "v2", prompts.vanilla_prompt(prompt_id).replace("{{" + name + "}}", ""))
    topic, rotation, guidance, angle = TOPICS[1], ROTATIONS[2], GUIDANCE[1], "An angle"
    goal = GOALS[2] if prompt_id in ("daily_cycle/ideation_goal", "daily_cycle/draft") else None
    invoke, sent = _sent(["A Good Title"])
    with invoke:
        if prompt_id == "daily_cycle/title":
            _, call, _ = handler._draft_title(angle, "m", None)
            expected = _legacy_title(angle, retry=False)
        elif prompt_id == "daily_cycle/draft":
            _, call = handler._draft_article(
                topic,
                angle,
                SUMMARIES,
                "m",
                None,
                guidance=guidance,
                few_shot_excerpt="Past.",
                goal=goal,
                rotation=rotation,
            )
            expected = _legacy_draft(topic, angle, SUMMARIES, guidance, "Past.", goal, rotation)
        else:
            titles = ["Old title"]
            _, call = handler._ideate(
                topic,
                SUMMARIES,
                "m",
                None,
                guidance=guidance,
                goal=goal,
                recent_titles=titles,
                rotation=rotation,
            )
            expected = _legacy_ideation(topic, SUMMARIES, guidance, goal, titles, rotation)

    assert sent == [expected]
    assert call["prompt_id"] == prompt_id and call["prompt_version"] == "vanilla:v1"
    out = capsys.readouterr().out.splitlines()
    lines = [evidence.parse(line) for line in out if line.startswith(evidence.PREFIX)]
    assert [line["code"] for line in lines if line["event"] == "prompt_fallback"] == ["missing_placeholder"]
