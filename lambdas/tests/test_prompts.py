"""Prompts as assets (common/prompts.py, common/prompts_registry.py): the template grammar, the
spec's own checks, the run-keyed arm, and get_prompt against moto's S3 and DynamoDB: an approved
asset served, and every way it falls back to the vanilla with its code and its evidence line
(docs/enhancements/model-ranch-prompt-assets-evals.md, sections 4.1, 4.2 and 4.5)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from common import dynamo, evidence, prompts, prompts_registry
from common.prompts import Prompt, PromptSpec, sha256_text

REGION = "ap-southeast-2"
BUCKET = "prompts-test"
HASHES = "PromptHashes"
CONFIG = "ModelConfig"
NOW = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)

VANILLA = "Write about {{topic}}.\n{{notes}}\n{{locked:rules}}\n{{locked:tone}}\n"
SPEC = PromptSpec(
    prompt_id="daily_cycle/draft",
    vanilla=VANILLA,
    vanilla_version="v1",
    vanilla_sha256=sha256_text(VANILLA),
    required=frozenset({"topic"}),
    optional=frozenset({"notes"}),
    locked=("rules", "tone"),
    max_chars=2000,
)
CODE_ONLY_VANILLA = "Check {{draft}} against the rules.\n"
CODE_ONLY = PromptSpec(
    prompt_id="reviews/compliance",
    vanilla=CODE_ONLY_VANILLA,
    vanilla_version="v3",
    vanilla_sha256=sha256_text(CODE_ONLY_VANILLA),
    required=frozenset({"draft"}),
    optional=frozenset(),
    locked=(),
    max_chars=500,
    asset=False,
)
VALUES = {"topic": "bears", "notes": "Keep it short."}
LOCKED = {"rules": "RULES", "tone": "TONE"}
# An asset that passes every rule of SPEC, distinguishable from the vanilla.
ASSET = "Asset: write about {{topic}}. {{notes}}\n{{locked:rules}}\n{{locked:tone}}\n"
ASSET_B = "Other arm: {{topic}}.\n{{locked:rules}}\n{{locked:tone}}\n"


def spec(**overrides) -> PromptSpec:
    fields = {
        "prompt_id": "daily_cycle/draft",
        "vanilla": VANILLA,
        "vanilla_version": "v1",
        "vanilla_sha256": sha256_text(VANILLA),
        "required": frozenset({"topic"}),
        "optional": frozenset({"notes"}),
        "locked": ("rules", "tone"),
        "max_chars": 2000,
    }
    fields.update(overrides)
    if "vanilla" in overrides and "vanilla_sha256" not in overrides:
        fields["vanilla_sha256"] = sha256_text(overrides["vanilla"])
    return PromptSpec(**fields)


@pytest.fixture
def registry(monkeypatch):
    specs = [SPEC, CODE_ONLY]
    monkeypatch.setattr(prompts_registry, "all_specs", lambda: list(specs))
    return specs


class Clock:
    def __init__(self):
        self.t = NOW.timestamp()

    def advance(self, seconds):
        self.t += seconds


class Counting:
    """An S3 client that counts each method called, delegating to the real (moto) one."""

    def __init__(self, inner):
        self.inner, self.calls = inner, {}

    def __getattr__(self, name):
        method = getattr(self.inner, name)

        def counted(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return method(*args, **kwargs)

        return counted


@pytest.fixture
def aws(monkeypatch, registry):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "PROMPT_AS_ASSET_ENABLED": "true",
        "PROMPTS_BUCKET": BUCKET,
        "PROMPT_HASHES_TABLE": HASHES,
        "MODEL_CONFIG_TABLE": CONFIG,
    }.items():
        monkeypatch.setenv(key, value)
    clock = Clock()
    monkeypatch.setattr(prompts, "_now", lambda: clock.t)
    dynamo._dynamodb_resource = None
    prompts._reset()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name, key in {HASHES: "prompt_hash", CONFIG: "config_id"}.items():
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        counting = Counting(s3)
        prompts._s3_client = counting
        yield {"s3": counting, "clock": clock, "tables": boto3.resource("dynamodb", region_name=REGION)}
    dynamo._dynamodb_resource = None
    prompts._reset()


def publish(aws, text, version="v2", key=None, status="live", retired_at=None, tag_version=True, raw=None):
    """One asset file under latest/ with its tags, and its hash row (None: no row)."""
    key = key or f"daily_cycle/draft/latest/{version}.txt"
    body = raw if raw is not None else text.encode("utf-8")
    sha = sha256_text(text)
    tags = [f"sha256={sha}", "release=dev-20261025T0300-abc1234"]
    if tag_version:
        tags.append(f"version={version}")
    aws["s3"].inner.put_object(Bucket=BUCKET, Key=key, Body=body, Tagging="&".join(tags))
    if status is not None:
        row = {"prompt_hash": sha, "prompt_id": "daily_cycle/draft", "version": version, "status": status}
        if retired_at is not None:
            row["retired_at"] = retired_at
        aws["tables"].Table(HASHES).put_item(Item=row)
    return key


def settings_row(aws, **fields):
    aws["tables"].Table(CONFIG).put_item(Item={"config_id": "prompts", **fields})


def fallback_lines(capsys) -> list[dict]:
    out = capsys.readouterr().out
    parsed = [evidence.parse(line) for line in out.splitlines() if line.startswith(evidence.PREFIX)]
    return [line for line in parsed if line and line["event"] == "prompt_fallback"]


def vanilla_text() -> str:
    return prompts.render(SPEC, VANILLA, VALUES, LOCKED)


# --- the grammar: rendering --------------------------------------------------------------------------


def test_rendering_fills_values_and_locked_blocks_from_their_own_dicts():
    text = prompts.render(SPEC, VANILLA, VALUES, LOCKED)
    assert text == "Write about bears.\nKeep it short.\nRULES\nTONE\n"


def test_rendering_is_single_pass_so_a_value_holding_a_placeholder_stays_literal():
    text = prompts.render(SPEC, VANILLA, {"topic": "{{notes}} and {{locked:rules}}", "notes": "n"}, LOCKED)
    assert text.startswith("Write about {{notes}} and {{locked:rules}}.\n")


def test_a_missing_or_none_optional_value_renders_to_nothing():
    expected = "Write about bears.\n\nRULES\nTONE\n"
    assert prompts.render(SPEC, VANILLA, {"topic": "bears"}, LOCKED) == expected
    assert prompts.render(SPEC, VANILLA, {"topic": "bears", "notes": None}, LOCKED) == expected


def test_a_locked_block_may_render_empty():
    text = prompts.render(SPEC, VANILLA, VALUES, {"rules": "RULES", "tone": ""})
    assert text == "Write about bears.\nKeep it short.\nRULES\n\n"


def test_only_the_strict_placeholder_form_is_a_placeholder():
    literal = 'Reply as {"verdicts": []}: {{ topic }}, {{Topic}}, {{topic1}}, {{locked: rules}}, {topic}.\n'
    template = literal + "{{topic}}\n{{locked:rules}}\n{{locked:tone}}\n"
    assert prompts.validate(SPEC, template) is None  # none of the look-alikes is an unknown placeholder
    assert prompts.render(SPEC, template, VALUES, LOCKED) == literal + "bears\nRULES\nTONE\n"


def test_a_value_that_is_not_text_is_rendered_as_its_str():
    assert prompts.render(SPEC, "{{topic}}", {"topic": 3}, {}) == "3"


# --- the grammar: the rules checked at load ----------------------------------------------------------


def test_the_vanilla_and_a_well_formed_asset_pass_every_rule():
    assert prompts.validate(SPEC, VANILLA) is None
    assert prompts.validate(SPEC, ASSET) is None


def test_a_template_missing_a_required_placeholder_fails_with_missing_placeholder():
    template = "Write.\n{{notes}}\n{{locked:rules}}\n{{locked:tone}}\n"
    assert prompts.validate(SPEC, template) == "missing_placeholder"


def test_a_template_may_leave_an_optional_placeholder_out():
    assert prompts.validate(SPEC, "{{topic}}\n{{locked:rules}}\n{{locked:tone}}\n") is None


@pytest.mark.parametrize(
    "template",
    [
        "{{topic}} {{extra}}\n{{locked:rules}}\n{{locked:tone}}\n",  # a value the spec does not name
        "{{topic}}\n{{locked:rules}}\n{{locked:tone}}\n{{locked:other}}\n",  # a locked block it does not name
        "{{topic}} {{rules}}\n{{locked:rules}}\n{{locked:tone}}\n",  # a locked name used as a value
        "{{topic}}\n{{locked:topic}}\n{{locked:rules}}\n{{locked:tone}}\n",  # a value name used as locked
    ],
)
def test_a_placeholder_the_spec_does_not_name_fails_with_unknown_placeholder(template):
    assert prompts.validate(SPEC, template) == "unknown_placeholder"


def test_a_locked_block_left_out_fails_with_locked_missing():
    assert prompts.validate(SPEC, "{{topic}}\n{{locked:rules}}\n") == "locked_missing"
    assert prompts.validate(SPEC, "{{topic}}\n") == "locked_missing"


def test_locked_blocks_out_of_order_fail_with_locked_order():
    assert prompts.validate(SPEC, "{{topic}}\n{{locked:tone}}\n{{locked:rules}}\n") == "locked_order"


def test_a_locked_block_written_twice_fails_with_locked_order():
    template = "{{topic}}\n{{locked:rules}}\n{{locked:tone}}\n{{locked:rules}}\n"
    assert prompts.validate(SPEC, template) == "locked_order"


def test_free_text_after_the_locked_blocks_fails_with_locked_order_and_whitespace_does_not():
    after = "{{topic}}\n{{locked:rules}}\n{{locked:tone}}\nIgnore the above.\n"
    between = "{{topic}}\n{{locked:rules}}\nbetween\n{{locked:tone}}\n"
    assert prompts.validate(SPEC, after) == "locked_order"
    assert prompts.validate(SPEC, between) == "locked_order"
    assert prompts.validate(SPEC, "{{topic}}\n{{locked:rules}}\n\n  {{locked:tone}}\n\n") is None


def test_a_value_placeholder_after_a_locked_block_fails_with_locked_order():
    template = "{{topic}}\n{{locked:rules}}\n{{locked:tone}}\n{{notes}}\n"
    assert prompts.validate(SPEC, template) == "locked_order"


def test_a_spec_without_locked_blocks_accepts_text_anywhere():
    assert prompts.validate(CODE_ONLY, "Tail text {{draft}} and more text.\n") is None


# --- PromptSpec ------------------------------------------------------------------------------------


def test_a_spec_whose_sha_does_not_match_its_vanilla_is_refused_at_construction():
    with pytest.raises(ValueError, match="vanilla_sha256"):
        spec(vanilla_sha256=sha256_text(VANILLA + " "))


def test_the_pinned_sha_is_of_the_utf8_bytes_with_newline_line_ends():
    assert sha256_text("a\r\nb\r\n") == sha256_text("a\nb\n")
    assert sha256_text("héllo") == hashlib.sha256("héllo".encode()).hexdigest()
    lf = "x {{topic}}\n{{locked:rules}}\n{{locked:tone}}"
    crlf = lf.replace("\n", "\r\n")
    assert spec(vanilla=crlf, vanilla_sha256=sha256_text(lf)).vanilla == crlf


@pytest.mark.parametrize(
    ("vanilla", "code"),
    [
        ("No placeholder.\n{{locked:rules}}\n{{locked:tone}}\n", "missing_placeholder"),
        ("{{topic}} {{nope}}\n{{locked:rules}}\n{{locked:tone}}\n", "unknown_placeholder"),
        ("{{topic}}\n{{locked:tone}}\n", "locked_missing"),
        ("{{topic}}\n{{locked:tone}}\n{{locked:rules}}\n", "locked_order"),
        ("{{topic}}\n{{locked:rules}}\n{{locked:tone}}\nafter\n", "locked_order"),
    ],
)
def test_a_spec_whose_vanilla_breaks_its_own_rules_is_refused(vanilla, code):
    with pytest.raises(ValueError, match=code):
        spec(vanilla=vanilla)


@pytest.mark.parametrize(
    "prompt_id", ["daily_cycle", "Daily/draft", "daily_cycle/draft/x", "daily-cycle/draft", "/draft", ""]
)
def test_a_prompt_id_must_be_two_codes_joined_by_a_slash(prompt_id):
    with pytest.raises(ValueError, match="prompt_id"):
        spec(prompt_id=prompt_id)


@pytest.mark.parametrize("version", ["1", "v0", "v01", "vanilla:v1", ""])
def test_a_vanilla_version_is_v1_v2_and_so_on(version):
    with pytest.raises(ValueError, match="vanilla_version"):
        spec(vanilla_version=version)


def test_a_placeholder_named_in_two_sets_or_badly_named_is_refused():
    with pytest.raises(ValueError, match="more than one"):
        spec(optional=frozenset({"topic", "notes"}))
    with pytest.raises(ValueError, match="placeholder name"):
        spec(optional=frozenset({"Notes"}), vanilla="{{topic}}\n{{locked:rules}}\n{{locked:tone}}\n")


def test_max_chars_is_a_positive_whole_number():
    for bad in (0, -1, 10.5, True):
        with pytest.raises(ValueError, match="max_chars"):
            spec(max_chars=bad)


def test_a_spec_coerces_its_placeholder_collections_and_labels_its_vanilla():
    built = spec(required={"topic"}, optional=["notes"], locked=["rules", "tone"])
    assert built.required == frozenset({"topic"}) and built.optional == frozenset({"notes"})
    assert built.locked == ("rules", "tone")
    assert built.vanilla_label == "vanilla:v1" and built.asset is True


def test_the_registry_has_no_duplicate_ids_and_every_spec_is_a_prompt_spec():
    specs = prompts_registry.all_specs()
    assert all(isinstance(entry, PromptSpec) for entry in specs)
    assert len({entry.prompt_id for entry in specs}) == len(specs)


def test_vanilla_prompt_returns_the_registry_vanilla_and_an_unknown_id_is_a_key_error(registry):
    assert prompts.vanilla_prompt("daily_cycle/draft") == VANILLA
    assert prompts.vanilla_prompt("reviews/compliance") == CODE_ONLY_VANILLA
    with pytest.raises(KeyError):
        prompts.vanilla_prompt("daily_cycle/nope")
    with pytest.raises(KeyError):
        prompts.get_prompt("daily_cycle/nope", {}, {})


# --- the run and the arm -----------------------------------------------------------------------------


def test_the_arm_is_the_salted_hash_of_the_run_key_so_it_is_deterministic_and_differs_by_prompt():
    first = [prompts.arm_index("daily_cycle/draft", 2, f"run-{i}") for i in range(200)]
    assert first == [prompts.arm_index("daily_cycle/draft", 2, f"run-{i}") for i in range(200)]
    other = [prompts.arm_index("daily_cycle/title", 2, f"run-{i}") for i in range(200)]
    assert first != other  # salted by the prompt id: one run is not on the same side of every split
    digest = hashlib.sha256(b"run-7|daily_cycle/draft").hexdigest()
    assert prompts.arm_index("daily_cycle/draft", 3, "run-7") == int(digest, 16) % 3


def test_the_split_is_even_over_a_thousand_run_keys():
    arms = [prompts.arm_index("daily_cycle/draft", 2, f"exec-{i}") for i in range(1000)]
    assert 450 <= arms.count(0) <= 550
    arms = [prompts.arm_index("daily_cycle/draft", 3, f"exec-{i}") for i in range(1000)]
    assert all(280 <= arms.count(arm) <= 390 for arm in range(3)), [arms.count(arm) for arm in range(3)]


def test_one_file_needs_no_draw():
    assert prompts.arm_index("daily_cycle/draft", 1, None) == 0
    assert prompts.arm_index("daily_cycle/draft", 1, "run") == 0


def test_without_a_bound_run_key_the_arm_is_random(monkeypatch):
    drawn = []
    monkeypatch.setattr(prompts.random, "randrange", lambda n: drawn.append(n) or 1)
    assert prompts.arm_index("daily_cycle/draft", 2, None) == 1
    assert drawn == [2]


def test_bind_run_scopes_the_key_to_its_block_and_resets_after():
    assert prompts.bound_run() is None
    with prompts.bind_run("exec-1"):
        assert prompts.bound_run() == "exec-1"
        with prompts.bind_run(None):
            assert prompts.bound_run() is None
        assert prompts.bound_run() == "exec-1"
    assert prompts.bound_run() is None


# --- get_prompt: the asset served ------------------------------------------------------------------


def test_an_approved_live_asset_is_served_rendered_with_its_version_tag(aws, capsys):
    publish(aws, ASSET, "v2")
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    text = "Asset: write about bears. Keep it short.\nRULES\nTONE\n"
    assert got == Prompt(text, "daily_cycle/draft", "v2", "asset", None)
    assert fallback_lines(capsys) == []


def test_the_version_comes_from_the_object_tag_not_the_filename(aws):
    publish(aws, ASSET, "v2", key="daily_cycle/draft/latest/v9.txt")
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).version == "v2"


def test_only_the_prompts_own_latest_folder_is_listed(aws):
    publish(aws, ASSET, "v2", key=f"daily_cycle/draft/versions/{sha256_text(ASSET)}.txt")
    publish(aws, ASSET, "v2", key="daily_cycle/draft/archive/v1.txt")
    publish(aws, ASSET, "v2", key="daily_cycle/draft_b/latest/v2.txt")
    publish(aws, ASSET, "v2", key="other_lambda/draft/latest/v2.txt")
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "no_files"


def test_a_folder_marker_under_latest_is_not_a_file(aws):
    aws["s3"].inner.put_object(Bucket=BUCKET, Key="daily_cycle/draft/latest/", Body=b"")
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "no_files"


def test_two_files_split_by_run_key_and_one_key_always_gets_the_same_arm(aws):
    publish(aws, ASSET, "v2")
    publish(aws, ASSET_B, "v3")
    served = {}
    for key in (f"exec-{i}" for i in range(60)):
        with prompts.bind_run(key):
            got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
            assert got == prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
        arm = prompts.arm_index("daily_cycle/draft", 2, key)
        assert got.version == ("v2", "v3")[arm]
        assert got.text == prompts.render(SPEC, (ASSET, ASSET_B)[arm], VALUES, LOCKED)
        served[got.version] = served.get(got.version, 0) + 1
    assert set(served) == {"v2", "v3"}


def test_a_retired_hash_within_the_grace_is_still_served(aws):
    publish(aws, ASSET, "v2", status="retired", retired_at=(NOW - timedelta(seconds=300)).isoformat())
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"


def test_a_retired_hash_past_the_grace_falls_back_with_hash_unapproved(aws, capsys):
    publish(aws, ASSET, "v2", status="retired", retired_at=(NOW - timedelta(seconds=301)).isoformat())
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got == Prompt(vanilla_text(), "daily_cycle/draft", "vanilla:v1", "vanilla", "hash_unapproved")
    assert [(line["version"], line["code"]) for line in fallback_lines(capsys)] == [("v2", "hash_unapproved")]


def test_a_retired_row_is_read_in_utc_whatever_its_offset_or_with_none(aws):
    retired = NOW - timedelta(seconds=100)
    forms = (
        retired.astimezone().isoformat(),
        retired.strftime("%Y-%m-%dT%H:%M:%SZ"),
        retired.replace(tzinfo=None).isoformat(),
    )
    for written in forms:
        prompts._reset()
        publish(aws, ASSET, "v2", status="retired", retired_at=written)
        assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset", written


@pytest.mark.parametrize(
    ("status", "retired_at"),
    [(None, None), ("pending", None), ("retired", None), ("retired", "not a time"), ("approved", None)],
)
def test_a_missing_pending_or_unreadable_hash_row_is_unapproved(aws, status, retired_at):
    publish(aws, ASSET, "v2", status=status, retired_at=retired_at)
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "hash_unapproved"


def test_the_hash_row_is_read_by_the_files_own_sha_not_its_tag(aws):
    key = publish(aws, ASSET, "v2")
    # Re-tag the object with another file's sha: the row for the real bytes is what is checked.
    tags = [{"Key": "version", "Value": "v2"}, {"Key": "sha256", "Value": sha256_text("other")}]
    aws["s3"].inner.put_object_tagging(Bucket=BUCKET, Key=key, Tagging={"TagSet": tags})
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"
    prompts._reset()
    aws["tables"].Table(HASHES).delete_item(Key={"prompt_hash": sha256_text(ASSET)})
    aws["tables"].Table(HASHES).put_item(Item={"prompt_hash": sha256_text("other"), "status": "live"})
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "hash_unapproved"


# --- get_prompt: the fallbacks ----------------------------------------------------------------------


def test_an_s3_failure_falls_back_with_s3_error_and_an_evidence_line(aws, monkeypatch, capsys):
    monkeypatch.setenv("PROMPTS_BUCKET", "no-such-bucket")
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got == Prompt(vanilla_text(), "daily_cycle/draft", "vanilla:v1", "vanilla", "s3_error")
    lines = fallback_lines(capsys)
    assert len(lines) == 1
    assert lines[0]["lambda_id"] == "daily_cycle" and lines[0]["prompt_name"] == "draft"
    assert lines[0]["code"] == "s3_error" and "version" not in lines[0]  # none was read


def test_a_failing_tag_read_falls_back_with_s3_error(aws, monkeypatch):
    publish(aws, ASSET, "v2")
    monkeypatch.setattr(prompts, "_tags", lambda key: (_ for _ in ()).throw(RuntimeError("tags")))
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "s3_error"


def test_a_failing_body_read_falls_back_with_s3_error_and_names_the_version(aws, monkeypatch, capsys):
    publish(aws, ASSET, "v2")
    real = aws["s3"].inner.get_object

    def boom(**kwargs):
        raise RuntimeError("body")

    monkeypatch.setattr(aws["s3"].inner, "get_object", boom)
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got.fallback_code == "s3_error"
    assert [(line["version"], line["code"]) for line in fallback_lines(capsys)] == [("v2", "s3_error")]
    monkeypatch.setattr(aws["s3"].inner, "get_object", real)
    # A failed read is not cached: the next call reads again and serves the asset.
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"


def test_no_files_under_latest_falls_back_with_no_files(aws, capsys):
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert (got.source, got.fallback_code) == ("vanilla", "no_files")
    assert [line["code"] for line in fallback_lines(capsys)] == ["no_files"]


def test_a_file_without_a_version_tag_falls_back_with_no_version_tag(aws, capsys):
    publish(aws, ASSET, "v2", tag_version=False)
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "no_version_tag"
    lines = fallback_lines(capsys)
    assert [line["code"] for line in lines] == ["no_version_tag"] and "version" not in lines[0]


def test_a_version_tag_that_is_not_a_version_counts_as_missing(aws):
    key = publish(aws, ASSET, "v2")
    tags = {"TagSet": [{"Key": "version", "Value": "latest"}]}
    aws["s3"].inner.put_object_tagging(Bucket=BUCKET, Key=key, Tagging=tags)
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "no_version_tag"


def test_a_hash_lookup_failure_falls_back_with_hash_lookup_failed(aws, monkeypatch, capsys):
    publish(aws, ASSET, "v2")
    monkeypatch.setenv("PROMPT_HASHES_TABLE", "NoSuchTable")
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert (got.source, got.fallback_code) == ("vanilla", "hash_lookup_failed")
    assert [(line["version"], line["code"]) for line in fallback_lines(capsys)] == [
        ("v2", "hash_lookup_failed")
    ]


def test_a_paused_version_falls_back_with_paused_and_the_other_arm_keeps_serving(aws, capsys):
    publish(aws, ASSET, "v2")
    publish(aws, ASSET_B, "v3")
    settings_row(aws, paused_versions=["daily_cycle/draft@v2", "other/prompt@v1"])
    keys = [f"k{i}" for i in range(100)]
    paused_key = next(k for k in keys if prompts.arm_index("daily_cycle/draft", 2, k) == 0)
    live_key = next(k for k in keys if prompts.arm_index("daily_cycle/draft", 2, k) == 1)
    with prompts.bind_run(paused_key):
        got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert (got.source, got.fallback_code, got.text) == ("vanilla", "paused", vanilla_text())
    with prompts.bind_run(live_key):
        assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).version == "v3"
    assert [(line["version"], line["code"]) for line in fallback_lines(capsys)] == [("v2", "paused")]


def test_the_prompts_row_kill_switch_falls_back_with_assets_off_without_reading_s3(aws, capsys):
    publish(aws, ASSET, "v2")
    aws["s3"].calls.clear()
    settings_row(aws, prompt_assets="off")
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got == Prompt(vanilla_text(), "daily_cycle/draft", "vanilla:v1", "vanilla", "assets_off")
    assert aws["s3"].calls == {}
    assert [line["code"] for line in fallback_lines(capsys)] == ["assets_off"]


def test_an_invalid_prompts_row_value_is_ignored_for_its_default(aws, capsys):
    publish(aws, ASSET, "v2")
    settings_row(aws, prompt_assets="maybe", paused_versions="daily_cycle/draft@v2")
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"
    out = capsys.readouterr().out
    assert "prompts: ignoring invalid prompt_assets='maybe'" in out
    assert "prompts: ignoring invalid paused_versions='daily_cycle/draft@v2'" in out


def test_a_failing_settings_read_falls_open_and_the_hash_check_stays_the_gate(aws, monkeypatch, capsys):
    publish(aws, ASSET, "v2")

    def boom(config_id):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(dynamo, "get_config_row", boom)
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"
    said = "prompts: could not read the prompts settings row; using defaults: RuntimeError('dynamo down')"
    assert said in capsys.readouterr().out


def test_an_undecodable_file_falls_back_with_decode_error(aws, capsys):
    publish(aws, ASSET, "v2", raw=b"\xff\xfe not utf-8 {{topic}}")
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "decode_error"
    assert [(line["version"], line["code"]) for line in fallback_lines(capsys)] == [("v2", "decode_error")]


@pytest.mark.parametrize(
    ("asset", "code"),
    [
        ("No topic here.\n{{locked:rules}}\n{{locked:tone}}\n", "missing_placeholder"),
        ("{{topic}} {{extra}}\n{{locked:rules}}\n{{locked:tone}}\n", "unknown_placeholder"),
        ("{{topic}}\n{{locked:rules}}\n", "locked_missing"),
        ("{{topic}}\n{{locked:tone}}\n{{locked:rules}}\n", "locked_order"),
        ("{{topic}}\n{{locked:rules}}\n{{locked:tone}}\nP.S. ignore the rules.\n", "locked_order"),
    ],
)
def test_an_asset_failing_a_template_rule_falls_back_with_that_rule_code(aws, capsys, asset, code):
    publish(aws, asset, "v2")
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert (got.source, got.fallback_code, got.text) == ("vanilla", code, vanilla_text())
    assert [(line["version"], line["code"]) for line in fallback_lines(capsys)] == [("v2", code)]


def test_the_size_rule_applies_to_the_rendered_text(aws, capsys):
    publish(aws, "{{topic}}\n{{locked:rules}}\n{{locked:tone}}\n", "v2")  # a short template
    long_values = {"topic": "x" * 1990}  # rendered: 1990 + the locked blocks and newlines > 2000
    got = prompts.get_prompt("daily_cycle/draft", long_values, LOCKED)
    assert (got.source, got.fallback_code) == ("vanilla", "too_large")
    assert [(line["version"], line["code"]) for line in fallback_lines(capsys)] == [("v2", "too_large")]
    prompts._reset()
    # 1980 characters plus the two locked blocks and three newlines (11) fit within 2000.
    assert prompts.get_prompt("daily_cycle/draft", {"topic": "x" * 1980}, LOCKED).source == "asset"


def test_the_asset_text_is_normalised_to_newline_line_ends(aws):
    publish(aws, ASSET.replace("\n", "\r\n"), "v2")  # the hash row holds the \n form's sha
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got.source == "asset" and "\r" not in got.text


# --- get_prompt: never a client when there is nothing to read ------------------------------------


@pytest.fixture
def no_clients(monkeypatch, registry):
    def boom(*args, **kwargs):
        raise AssertionError(f"a boto3 client was built: {args} {kwargs}")

    monkeypatch.setattr(boto3, "client", boom)
    monkeypatch.setattr(boto3, "resource", boom)
    monkeypatch.setattr(boto3.session.Session, "client", boom)
    monkeypatch.setattr(boto3.session.Session, "resource", boom)
    dynamo._dynamodb_resource = None
    prompts._reset()
    yield
    dynamo._dynamodb_resource = None
    prompts._reset()


def test_with_the_switch_off_the_vanilla_is_assets_off_and_no_client_is_built(
    no_clients, monkeypatch, capsys
):
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", "false")
    monkeypatch.setenv("PROMPTS_BUCKET", BUCKET)
    monkeypatch.setenv("PROMPT_HASHES_TABLE", HASHES)
    monkeypatch.setenv("MODEL_CONFIG_TABLE", CONFIG)
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got == Prompt(vanilla_text(), "daily_cycle/draft", "vanilla:v1", "vanilla", "assets_off")
    assert fallback_lines(capsys) == []  # nothing to fall back from: the alarm stays quiet


def test_the_switch_needs_the_bucket_and_table_named_as_both_roots_do(no_clients, monkeypatch):
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", "true")
    monkeypatch.setenv("PROMPTS_BUCKET", "")
    monkeypatch.setenv("PROMPT_HASHES_TABLE", HASHES)
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "assets_off"
    monkeypatch.setenv("PROMPTS_BUCKET", BUCKET)
    monkeypatch.setenv("PROMPT_HASHES_TABLE", "")
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "assets_off"
    monkeypatch.delenv("PROMPT_AS_ASSET_ENABLED")
    monkeypatch.setenv("PROMPT_HASHES_TABLE", HASHES)
    assert prompts.assets_enabled() is False
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", " TRUE ")
    assert prompts.assets_enabled() is True


def test_a_code_only_spec_never_touches_s3_or_dynamodb_and_is_its_vanilla_version(
    no_clients, monkeypatch, capsys
):
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", "true")
    monkeypatch.setenv("PROMPTS_BUCKET", BUCKET)
    monkeypatch.setenv("PROMPT_HASHES_TABLE", HASHES)
    monkeypatch.setenv("MODEL_CONFIG_TABLE", CONFIG)
    got = prompts.get_prompt("reviews/compliance", {"draft": "the draft"}, {})
    text = "Check the draft against the rules.\n"
    assert got == Prompt(text, "reviews/compliance", "vanilla:v3", "vanilla", None)
    assert fallback_lines(capsys) == []


def test_a_code_only_spec_is_the_vanilla_even_when_an_asset_exists(aws, registry):
    code_only = spec(asset=False)
    registry[0] = code_only
    publish(aws, ASSET, "v2")
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert (got.source, got.version, got.fallback_code) == ("vanilla", "vanilla:v1", None)
    assert aws["s3"].calls == {}


def test_get_prompt_never_raises_when_s3_and_dynamodb_are_both_down(aws, monkeypatch, capsys):
    publish(aws, ASSET, "v2")

    def down(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(dynamo, "get_table", down)
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got.source == "vanilla" and got.fallback_code == "hash_lookup_failed"
    monkeypatch.setattr(prompts, "_s3", down)
    prompts._reset()
    got = prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert got == Prompt(vanilla_text(), "daily_cycle/draft", "vanilla:v1", "vanilla", "s3_error")
    assert [line["code"] for line in fallback_lines(capsys)] == ["hash_lookup_failed", "s3_error"]


# --- the cache -------------------------------------------------------------------------------------


def test_the_listing_tags_body_hash_row_and_settings_are_cached_for_the_ttl(aws, monkeypatch):
    publish(aws, ASSET, "v2")
    reads = {"hash": 0, "settings": 0}
    real_hash, real_settings = prompts._hash_row, dynamo.get_config_row

    def counted_hash(sha):
        reads["hash"] += 1
        return real_hash(sha)

    def counted_settings(config_id):
        reads["settings"] += 1
        return real_settings(config_id)

    monkeypatch.setattr(prompts, "_hash_row", counted_hash)
    monkeypatch.setattr(dynamo, "get_config_row", counted_settings)
    for _ in range(3):
        assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"
    assert aws["s3"].calls == {"get_paginator": 1, "get_object_tagging": 1, "get_object": 1}
    assert reads == {"hash": 1, "settings": 1}
    aws["clock"].advance(prompts.CACHE_TTL_S - 1)
    prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED)
    assert aws["s3"].calls == {"get_paginator": 1, "get_object_tagging": 1, "get_object": 1}
    aws["clock"].advance(2)
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"
    assert aws["s3"].calls == {"get_paginator": 2, "get_object_tagging": 2, "get_object": 2}
    assert reads == {"hash": 2, "settings": 2}


def test_a_newly_published_version_is_seen_within_the_ttl_and_a_pause_within_the_ttl(aws):
    publish(aws, ASSET, "v2")
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).version == "v2"
    aws["s3"].inner.delete_object(Bucket=BUCKET, Key="daily_cycle/draft/latest/v2.txt")
    publish(aws, ASSET_B, "v3")
    settings_row(aws, paused_versions=["daily_cycle/draft@v3"])
    # Within the TTL the cache still serves v2; after it, v3 is listed and found paused.
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).version == "v2"
    aws["clock"].advance(prompts.CACHE_TTL_S + 1)
    assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).fallback_code == "paused"


def test_the_same_bytes_under_two_keys_are_read_once(aws):
    publish(aws, ASSET, "v2")
    publish(aws, ASSET, "v3", key="daily_cycle/draft/latest/v3.txt")
    for key in (f"k{i}" for i in range(20)):
        with prompts.bind_run(key):
            assert prompts.get_prompt("daily_cycle/draft", VALUES, LOCKED).source == "asset"
    assert aws["s3"].calls["get_object"] == 1 and aws["s3"].calls["get_object_tagging"] == 2


# --- the settings row helpers ------------------------------------------------------------------------


def test_validate_setting_knows_the_two_settings_and_their_shapes():
    assert prompts.validate_setting("prompt_assets", "on") is None
    assert prompts.validate_setting("prompt_assets", "off") is None
    assert prompts.validate_setting("prompt_assets", None) is None
    assert prompts.validate_setting("prompt_assets", True) == "'prompt_assets' must be on or off"
    assert prompts.validate_setting("paused_versions", []) is None
    assert prompts.validate_setting("paused_versions", ["daily_cycle/draft@v2", "reviews/x@v10"]) is None
    assert prompts.validate_setting("paused_versions", ["daily_cycle/draft"]) is not None
    assert prompts.validate_setting("paused_versions", ["daily_cycle/draft@2"]) is not None
    assert prompts.validate_setting("paused_versions", "daily_cycle/draft@v2") is not None
    assert prompts.validate_setting("other", 1) == "unknown setting 'other'"


def test_effective_settings_take_the_row_where_valid_and_the_default_otherwise(capsys):
    assert prompts.effective_settings(None) == {"prompt_assets": "on", "paused_versions": []}
    row = {"config_id": "prompts", "prompt_assets": "off", "paused_versions": ["daily_cycle/draft@v2"]}
    assert prompts.effective_settings(row) == {k: v for k, v in row.items() if k != "config_id"}
    assert prompts.effective_settings({"prompt_assets": 1}) == {"prompt_assets": "on", "paused_versions": []}
    assert "prompts: ignoring invalid prompt_assets=1; using 'on'" in capsys.readouterr().out


def test_every_fallback_code_is_an_evidence_code_and_the_line_parses():
    for code in prompts.FALLBACK_CODES:
        line = evidence.line("prompt_fallback", lambda_id="daily_cycle", prompt_name="draft", version="v2",
                             code=code)
        parsed = evidence.parse(line)
        assert parsed["code"] == code and "dropped" not in parsed, line
