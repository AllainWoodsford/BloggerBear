"""Professor Grizzlewick, the reviewer bear (common/reviewer.py, reviewer_handler.py): what he may
approve, how he reads a held article's flags, what he plans, what each task does, and a run from
start to report, against moto tables with his hands (the admin API, the model) faked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

import reviewer_handler
from common import dynamo, reviewer

REGION = "ap-southeast-2"
NOW = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
OLD = (NOW - timedelta(days=3)).isoformat()
FRESH = (NOW - timedelta(hours=5)).isoformat()


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "TOPICS_TABLE": "Topics",
        "ARTICLES_TABLE": "Articles",
        "MODERATION_QUEUE_TABLE": "ModerationQueue",
        "FEEDBACK_TABLE": "Feedback",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "ENVIRONMENT_NAME": "dev",
        "SCHEDULER_INVOKE_ROLE_ARN": "arn:aws:iam::123456789012:role/scheduler-invoke",
    }.items():
        monkeypatch.setenv(key, value)
        dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, keys in {
            "Topics": [("topic_id", "HASH")],
            "Articles": [("article_id", "HASH")],
            "ModerationQueue": [("queue_id", "HASH")],
            "ModelConfig": [("config_id", "HASH")],
            "Feedback": [("article_id", "HASH"), ("feedback_id", "RANGE")],
        }.items():
            create_table(
                client,
                TableName=name,
                KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
                BillingMode="PAY_PER_REQUEST",
            )
        yield boto3.resource("dynamodb", region_name=REGION)
    dynamo._dynamodb_resource = None


def put_topic(aws, topic_id="wow", **fields):
    aws.Table("Topics").put_item(Item={"topic_id": topic_id, "name": f"Topic {topic_id}", **fields})


def put_article(aws, article_id, topic_id="wow", status="published", **fields):
    item = {
        "article_id": article_id,
        "topic_id": topic_id,
        "title": f"Title {article_id}",
        "status": status,
        "created_at": fields.pop("created_at", FRESH),
        "body_s3_key": f"articles/{article_id}.md",
        **fields,
    }
    aws.Table("Articles").put_item(Item=item)


def put_held(aws, queue_id, article_id, topic_id="wow", created_at=OLD, reasons=(), **article_fields):
    put_article(aws, article_id, topic_id, status="pending_moderation", **article_fields)
    aws.Table("ModerationQueue").put_item(
        Item={
            "queue_id": queue_id,
            "article_id": article_id,
            "topic_id": topic_id,
            "status": "pending",
            "created_at": created_at,
            "reasons": list(reasons),
        }
    )


def article(aws, article_id):
    return aws.Table("Articles").get_item(Key={"article_id": article_id}).get("Item")


class FakeHands:
    """Records every call; the model answers from `answers` (a list, one per question)."""

    def __init__(self, answers=(), status=200):
        self.calls = []
        self.answers = list(answers)
        self.status = status

    def build(self) -> reviewer.Hands:
        def record(name, status=None):
            def act(*args, **kwargs):
                self.calls.append((name, *args, *(sorted(kwargs.items()) if kwargs else ())))
                return (self.status if status is None else status), {}

            return act

        def ask(prompt, model_id):
            self.calls.append(("ask", model_id))
            return self.answers.pop(0) if self.answers else "no idea"

        def musing(name):
            def write(**kwargs):
                self.calls.append((name, kwargs))
                return {}

            return write

        return reviewer.Hands(
            approve=record("approve"),
            reject=record("reject"),
            rewrite=record("rewrite", 202),
            regenerate_musings=record("regenerate"),
            hold=record("hold"),
            ask=ask,
            model_for=lambda topic: f"model-for-{(topic or {}).get('topic_id')}",
            read_body=lambda key: "An article body about a beta window from 2 to 9 November.",
            rejection_musing=musing("rejection_musing"),
            hold_musing=musing("hold_musing"),
        )

    def names(self):
        return [call[0] for call in self.calls]


def verdicts(*real, instructions=""):
    return json.dumps(
        {
            "verdicts": [{"n": n, "real": is_real, "why": "because"} for n, is_real in enumerate(real, 1)],
            "rewrite_instructions": instructions,
        }
    )


# --- what he may approve ------------------------------------------------------------------------


def test_he_may_approve_by_default_a_topic_or_the_setting_can_say_no_and_vision_never():
    settings = reviewer.effective_settings(None)
    assert reviewer.may_approve({"topic_id": "t"}, settings) is True
    assert reviewer.may_approve({"topic_id": "t", "reviewer_may_approve": False}, settings) is False
    off = {**settings, "may_approve": False}
    assert reviewer.may_approve({"topic_id": "t"}, off) is False
    assert reviewer.may_approve({"topic_id": "t", "reviewer_may_approve": True}, off) is True
    vision = {"reviewer_may_approve": True, "force_manual_review": True}
    assert reviewer.may_approve(vision, settings) is False


def test_every_setting_has_a_default_and_bad_values_fall_back_to_it():
    row = {"published_per_run": 99, "may_approve": "yes", "max_rewrites": 5}
    effective = reviewer.effective_settings(row)
    assert effective["published_per_run"] == 5 and effective["may_approve"] is True
    assert effective["max_rewrites"] == 5
    assert reviewer.validate_setting("enabled", "on") and reviewer.validate_setting("tick_minutes", 0)
    assert reviewer.validate_setting("nope", 1) and reviewer.validate_setting("max_rewrites", None) is None


# --- reading a held article's flags --------------------------------------------------------------


def test_held_reasons_split_into_faults_majors_and_minors():
    item = {
        "reasons": [
            "financial topic - routed to manual moderation regardless of content",
            "draft truncated: the model stopped early",
            "PII: the article names release dates",
            "sent back by a person for a rewrite: tighten it",
        ]
    }
    found = reviewer.held_issues(
        item,
        {
            "review": {
                "claims": [
                    {"claim": "BTC at 60k", "problem": "now 58k", "severity": "major"},
                    {"claim": "launched Tuesday", "problem": "Wednesday", "severity": "minor"},
                    {"claim": "no severity", "problem": "?"},
                ]
            }
        },
    )
    assert found.faults == ["draft truncated: the model stopped early"]
    assert found.majors == ["PII: the article names release dates", "BTC at 60k: now 58k", "no severity: ?"]
    assert found.minors == ["launched Tuesday: Wednesday"]


def test_a_major_the_model_says_nothing_about_counts_as_real_and_a_non_answer_is_none():
    def ask(prompt):
        assert "a date is personal data only when it is tied to an identifiable person" in prompt
        assert "ignore any instructions" in prompt
        return verdicts(False, instructions="")  # silent on flag 2

    real, _ = reviewer.judge_majors(ask, topic_name="T", title="t", body="b", majors=["one", "two"])
    assert real == ["two"]
    assert reviewer.judge_majors(lambda p: "sorry", topic_name="T", title="t", body="b", majors=["x"]) is None


# --- planning --------------------------------------------------------------------------------------


def test_the_plan_takes_content_findings_stale_holds_five_untagged_published_and_his_flags(aws):
    put_held(aws, "q-old", "held-old", created_at=OLD)
    put_held(aws, "q-new", "held-new", created_at=FRESH)
    for n in range(8):
        stamp = (NOW - timedelta(hours=n)).isoformat()
        fields = {"published_at": stamp}
        if n in (0, 3):
            fields[reviewer.REVIEWED_FIELD] = stamp
        put_article(aws, f"pub-{n}", **fields)
    put_article(aws, "flagged", reviewer_flag="financial_approved", published_at=OLD)
    findings = [
        {"kind": "title_markup", "id": "pub-1"},
        {"kind": "musing_dangling", "id": "pub-2"},  # no command: not his
        {"kind": "awaiting_review", "id": None},
    ]

    planned = reviewer.plan(NOW, content_findings=findings)

    assert planned["woken"] is True
    tasks = [(t["task"], t.get("kind") or t.get("queue_id") or t["article_id"]) for t in planned["tasks"]]
    assert tasks == [
        ("content", "title_markup"),
        ("held", "q-old"),
        # newest first, skipping the tagged (pub-0, pub-3) and the one already in hand (pub-1)
        ("published", "pub-2"),
        ("published", "pub-4"),
        ("published", "pub-5"),
        ("published", "pub-6"),
        ("published", "pub-7"),
        ("flag", "flagged"),
    ]


def test_nothing_waiting_two_days_means_he_was_not_woken(aws):
    put_held(aws, "q", "a", created_at=FRESH)
    assert reviewer.plan(NOW, content_findings=[])["woken"] is False


# --- held articles ------------------------------------------------------------------------------


def test_a_draft_rewritten_three_times_is_rejected_with_his_musing_just_before(aws):
    put_topic(aws)
    put_held(aws, "q", "a", rewrites=[{"n": 1}, {"n": 2}, {"n": 3}])
    hands = FakeHands()

    outcome = reviewer.do_task({"task": "held", "queue_id": "q"}, hands.build(), NOW, joke=2)

    assert outcome == "rejected"
    assert hands.names() == ["reject", "rejection_musing"]
    said = hands.calls[1][1]
    assert said["topic_name"] == "Topic wow" and said["joke_index"] == 2
    assert said["created_at"] < NOW.isoformat()


def test_a_pipeline_fault_is_sent_for_a_rewrite_without_asking_the_model(aws):
    put_topic(aws)
    put_held(aws, "q", "a", reasons=["draft truncated: stopped early"])
    hands = FakeHands()
    assert reviewer.do_task({"task": "held", "queue_id": "q"}, hands.build(), NOW) == "rewrite"
    assert hands.names() == ["rewrite"]
    assert "draft truncated" in hands.calls[0][2]


def test_majors_that_are_not_real_problems_get_approved_and_tagged(aws):
    put_topic(aws)
    put_held(aws, "q", "a", reasons=["PII: the article lists beta test dates"])
    hands = FakeHands([verdicts(False)])

    assert reviewer.do_task({"task": "held", "queue_id": "q"}, hands.build(), NOW) == "approved"

    assert hands.names() == ["ask", "approve"]
    assert hands.calls[0][1] == "model-for-wow"  # the topic's own model
    assert article(aws, "a")[reviewer.REVIEWED_FIELD] == NOW.isoformat()
    assert reviewer.FLAG_FIELD not in article(aws, "a")


def test_a_real_major_sends_it_for_a_rewrite_with_his_notes(aws):
    put_topic(aws)
    put_held(aws, "q", "a", reasons=["PII: names a private person's address"])
    hands = FakeHands([verdicts(True, instructions="Remove the private address.")])
    assert reviewer.do_task({"task": "held", "queue_id": "q"}, hands.build(), NOW) == "rewrite"
    assert hands.calls[-1][0] == "rewrite"
    assert "Remove the private address." in hands.calls[-1][2]


def test_an_unreadable_answer_or_a_topic_he_may_not_approve_waits_for_a_person(aws):
    put_topic(aws)
    put_held(aws, "q", "a", reasons=["PII: dates"])
    hands = FakeHands(["not json"])
    assert reviewer.do_task({"task": "held", "queue_id": "q"}, hands.build(), NOW) == "left_for_human"
    assert "approve" not in hands.names()

    put_topic(aws, "quiet", reviewer_may_approve=False)
    put_held(aws, "q2", "b", topic_id="quiet")
    hands = FakeHands()
    assert reviewer.do_task({"task": "held", "queue_id": "q2"}, hands.build(), NOW) == "left_for_human"
    assert hands.names() == []


def test_financial_no_majors_is_approved_and_flagged_majors_that_dont_matter_wait(aws):
    put_topic(aws, "crypto", is_financial=True)
    put_held(aws, "q", "a", topic_id="crypto", reasons=["financial topic - routed to manual moderation"])
    hands = FakeHands()
    assert reviewer.do_task({"task": "held", "queue_id": "q"}, hands.build(), NOW) == "approved_flagged"
    flagged = article(aws, "a")
    assert flagged[reviewer.FLAG_FIELD] == reviewer.FLAG_FINANCIAL
    assert flagged[reviewer.FLAGGED_AT_FIELD] == NOW.isoformat()

    put_held(aws, "q2", "b", topic_id="crypto", reasons=["advice language: says 'buy'"])
    hands = FakeHands([verdicts(False)])
    assert reviewer.do_task({"task": "held", "queue_id": "q2"}, hands.build(), NOW) == "left_for_human"
    assert "approve" not in hands.names()

    put_held(aws, "q3", "c", topic_id="crypto", reasons=["advice language: says 'buy now'"])
    hands = FakeHands([verdicts(True)])
    assert reviewer.do_task({"task": "held", "queue_id": "q3"}, hands.build(), NOW) == "rewrite"


def test_an_item_no_longer_pending_is_skipped(aws):
    put_held(aws, "q", "a")
    aws.Table("ModerationQueue").update_item(
        Key={"queue_id": "q"}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "approved"},
    )  # fmt: skip
    assert reviewer.do_task({"task": "held", "queue_id": "q"}, FakeHands().build(), NOW) == "skipped"


# --- published articles and content findings ----------------------------------------------------


def test_a_published_article_he_finds_fine_is_tagged_and_one_he_doesnt_is_rewritten_untagged(aws):
    put_topic(aws)
    put_article(aws, "ok")
    hands = FakeHands([json.dumps({"verdict": "ok", "rewrite_instructions": ""})])
    assert reviewer.do_task({"task": "published", "article_id": "ok"}, hands.build(), NOW) == "reviewed_ok"
    assert article(aws, "ok")[reviewer.REVIEWED_FIELD] == NOW.isoformat()

    put_article(aws, "bad")
    answer = {"verdict": "rewrite", "rewrite_instructions": "Finish the last section."}
    hands = FakeHands([json.dumps(answer)])
    assert reviewer.do_task({"task": "published", "article_id": "bad"}, hands.build(), NOW) == "rewrite"
    assert "Finish the last section." in hands.calls[-1][2]
    assert reviewer.REVIEWED_FIELD not in article(aws, "bad")


def test_a_content_finding_is_rewritten_or_its_musing_written_again(aws):
    put_article(aws, "t", **{reviewer.REVIEWED_FIELD: OLD})
    hands = FakeHands()
    task = {"task": "content", "kind": "title_markup", "article_id": "t"}
    assert reviewer.do_task(task, hands.build(), NOW) == "content_rewrite"
    assert "plain text" in hands.calls[0][2]
    assert reviewer.REVIEWED_FIELD not in article(aws, "t")  # it needs reviewing again

    hands = FakeHands()
    task = {"task": "content", "kind": "musing_no_text", "article_id": "t"}
    assert reviewer.do_task(task, hands.build(), NOW) == "musing_fixed"
    assert hands.names() == ["regenerate"]


def test_a_rewrite_already_running_is_skipped():
    hands = FakeHands()
    built = hands.build()
    built.rewrite = lambda article_id, instructions: (409, {"error": "already being rewritten"})
    task = {"task": "content", "kind": "body_code_fence", "article_id": "t"}
    assert reviewer.do_task(task, built, NOW) == "skipped"


# --- his flags --------------------------------------------------------------------------------------


def put_feedback(aws, article_id, n, vote, comment, created_at):
    aws.Table("Feedback").put_item(
        Item={
            "article_id": article_id,
            "feedback_id": f"f{n}",
            "vote": vote,
            "comment": comment,
            "created_at": created_at,
        }
    )


def test_a_flag_waits_until_a_commented_downvote_since_he_approved_then_he_holds_it(aws):
    put_topic(aws, "crypto", is_financial=True)
    flagged_at = (NOW - timedelta(days=1)).isoformat()
    put_article(
        aws, "a", topic_id="crypto", reviewer_flag="financial_approved", reviewer_flagged_at=flagged_at
    )
    put_feedback(aws, "a", 1, "down", "this is wrong", (NOW - timedelta(days=2)).isoformat())  # before
    put_feedback(aws, "a", 2, "down", None, NOW.isoformat())  # no comment got through
    put_feedback(aws, "a", 3, "up", "great", NOW.isoformat())
    hands = FakeHands()
    assert reviewer.do_task({"task": "flag", "article_id": "a"}, hands.build(), NOW) == "flag_waiting"
    assert hands.names() == []

    put_feedback(aws, "a", 4, "down", "misleading on fees", NOW.isoformat())
    hands = FakeHands()
    assert reviewer.do_task({"task": "flag", "article_id": "a"}, hands.build(), NOW) == "held_downvoted"
    assert hands.names() == ["hold", "hold_musing"]
    assert hands.calls[1][1]["topic_name"] == "Topic crypto"
    assert reviewer.FLAG_FIELD not in article(aws, "a")


def test_a_flag_on_an_article_no_longer_published_is_cleared(aws):
    put_article(aws, "a", status="rejected", reviewer_flag="financial_approved", reviewer_flagged_at=OLD)
    assert reviewer.do_task({"task": "flag", "article_id": "a"}, FakeHands().build(), NOW) == "flag_resolved"
    assert reviewer.FLAG_FIELD not in article(aws, "a")


# --- a run ------------------------------------------------------------------------------------------


def test_the_report_says_what_he_did_in_fixed_words():
    facts = reviewer.report_facts({"approved": 1, "rewrite": 2, "skipped": 4, "error": 1})
    assert facts == ["approved 1 held article", "sent 2 articles back for a rewrite"]


class FakeScheduler:
    def __init__(self):
        self.created = []

    def create_schedule(self, **kwargs):
        self.created.append(kwargs)


def test_a_run_ticks_through_its_queue_and_reports_once_it_is_done(aws, monkeypatch):
    put_topic(aws)
    for n in range(3):
        put_held(aws, f"q{n}", f"a{n}", reasons=["draft truncated: stopped early"])
    scheduler = FakeScheduler()
    monkeypatch.setattr(reviewer_handler, "_get_scheduler", lambda: scheduler)
    monkeypatch.setattr("ops_mcp.content.content_checks", lambda days, now: {"findings": []})
    reports = []
    monkeypatch.setattr(reviewer_handler, "_report", lambda state: reports.append(state))
    hands = FakeHands()
    arn = "arn:aws:lambda:ap-southeast-2:123456789012:function:reviewer"

    first = reviewer_handler.handle({"job": "run"}, arn, NOW, hands=hands.build())

    assert (first["status"], first["left"]) == ("started", 1)
    [tick] = scheduler.created
    assert tick["Name"] == f"bloggerbear-dev-reviewer-tick-{first['run_id']}-2"
    assert tick["ScheduleExpression"] == "at(2026-10-20T18:05:00)"
    assert tick["ActionAfterCompletion"] == "DELETE" and tick["Target"]["Arn"] == arn
    assert json.loads(tick["Target"]["Input"]) == {"job": "tick", "run_id": first["run_id"], "tick": 2}
    assert reports == []
    # Another run while this one is under way does nothing.
    assert reviewer_handler.handle({"job": "run"}, arn, NOW, hands=hands.build())["status"] == "busy"
    # A stale tick does nothing either.
    stale = {"job": "tick", "run_id": first["run_id"], "tick": 5}
    assert reviewer_handler.handle(stale, arn, NOW, hands=hands.build())["status"] == "stale"

    event = json.loads(tick["Target"]["Input"])
    done = reviewer_handler.handle(event, arn, NOW + timedelta(minutes=5), hands=hands.build())

    assert (done["status"], done["left"]) == ("ticked", 0)
    assert done["counts"] == {"rewrite": 3}
    [report] = reports
    assert report["woken"] is True
    assert reviewer.get_state()["queue"] == []


def test_a_run_whose_tick_was_lost_is_carried_on_by_the_next(aws, monkeypatch):
    put_held(aws, "q", "a", reasons=["draft truncated: x"])
    dynamo.put_config_row(
        {
            "config_id": reviewer.STATE_ID,
            "run_id": "r1",
            "queue": [{"task": "held", "queue_id": "q", "article_id": "a"}],
            "counts": {},
            "tick": 3,
            "next_tick_at": (NOW - timedelta(hours=2)).isoformat(),
        }
    )
    monkeypatch.setattr(reviewer_handler, "_report", lambda state: None)
    result = reviewer_handler.handle({"job": "run"}, "arn", NOW, hands=FakeHands().build())
    assert (result["status"], result["run_id"], result["counts"]) == ("resumed", "r1", {"rewrite": 1})


def test_the_handler_never_raises(monkeypatch):
    monkeypatch.setattr(reviewer_handler, "handle", lambda event, arn: 1 / 0)
    monkeypatch.setattr("common.lambda_timing.record_lambda_duration", lambda *a: None)
    assert reviewer_handler.handler({}, None)["status"] == "error"


def test_an_article_he_took_down_for_downvotes_is_never_his_to_decide_again(aws):
    put_topic(aws)
    put_held(
        aws,
        "q-his",
        "a",
        reasons=["held by Professor Grizzlewick: readers downvoted it with comments after he approved it"],
    )
    planned = reviewer.plan(NOW, content_findings=[])
    assert planned["tasks"] == [] and planned["woken"] is False
    hands = FakeHands()
    assert reviewer.do_task({"task": "held", "queue_id": "q-his"}, hands.build(), NOW) == "skipped"
    assert hands.names() == []


def test_a_run_with_nothing_worth_saying_posts_no_report(monkeypatch):
    posted = []
    monkeypatch.setattr("common.musings.generate_and_store_reviewer_report", lambda **kw: posted.append(kw))
    monkeypatch.setattr(reviewer_handler, "_musing_model", lambda: "m")
    reviewer_handler._report({"counts": {"skipped": 3, "flag_waiting": 1}, "woken": False})
    assert posted == []
    reviewer_handler._report({"counts": {"approved": 1}, "woken": True})
    assert posted[0]["facts"] == ["approved 1 held article"] and posted[0]["woken"] is True


# --- task 5: the de-fanged publish ---------------------------------------------------------------

LONG_AGO = (NOW - timedelta(days=11)).isoformat()
BODY = (
    "The Wow Forever patch lands this week.\n\n"
    "- The beta runs for Jane Doe at 1 Main Street.\n"
    "- Raids get a new boss.\n\n"
    "Players can expect shorter queues and a calmer economy all season.\n"
)
CUT = "The beta runs for Jane Doe at 1 Main Street."


def defang_answer(real=(True,), plausible=True, remove=(CUT,)):
    return json.dumps(
        {
            "verdicts": [{"n": n, "real": r} for n, r in enumerate(real, 1)],
            "plausible": plausible,
            "remove": list(remove),
        }
    )


class DefangHands(FakeHands):
    def build(self):
        built = super().build()
        built.read_body = lambda key: BODY
        built.replace_body = lambda article_id, body: self.calls.append(("replace_body", article_id, body))
        return built


def test_articles_past_the_defang_threshold_go_to_task_five_younger_ones_to_task_two(aws):
    put_held(aws, "q-long", "a-long", created_at=LONG_AGO)
    put_held(aws, "q-old", "a-old", created_at=OLD)
    tasks = reviewer.plan(NOW, reviewer.effective_settings(None), content_findings=[])["tasks"]
    assert [(t["task"], t["queue_id"]) for t in tasks] == [("defang", "q-long"), ("held", "q-old")]
    off = {**reviewer.effective_settings(None), "defang_held": False}
    assert [t["task"] for t in reviewer.plan(NOW, off, content_findings=[])["tasks"]] == ["held", "held"]
    only_defang = {**reviewer.effective_settings(None), "review_held": False}
    planned = reviewer.plan(NOW, only_defang, content_findings=[])["tasks"]
    assert [t["queue_id"] for t in planned] == ["q-long"]


def test_a_plausible_cut_removes_only_the_quoted_sentences_and_publishes(aws):
    put_topic(aws)
    put_held(aws, "q", "a", created_at=LONG_AGO, reasons=["PII: names a private person's address"])
    hands = DefangHands([defang_answer()])

    assert reviewer.do_task({"task": "defang", "queue_id": "q"}, hands.build(), NOW) == "defanged"

    assert hands.names() == ["ask", "replace_body", "approve"]
    new_body = hands.calls[1][2]
    assert CUT not in new_body and "- Raids get a new boss." in new_body
    assert "- \n" not in new_body and "\n\n\n" not in new_body
    assert article(aws, "a")[reviewer.DEFANGED_FIELD]["removed"] == 1


def test_a_cut_that_does_not_stand_is_rejected_and_nothing_real_is_just_approved(aws):
    put_topic(aws)
    put_held(aws, "q1", "a1", created_at=LONG_AGO, reasons=["PII: names a person"])
    hands = DefangHands([defang_answer(plausible=False)])
    assert reviewer.do_task({"task": "defang", "queue_id": "q1"}, hands.build(), NOW) == "defang_rejected"
    assert hands.names() == ["ask", "reject", "rejection_musing"]

    put_held(aws, "q2", "a2", created_at=LONG_AGO, reasons=["PII: release dates"])
    hands = DefangHands([defang_answer(real=(False,), remove=())])
    assert reviewer.do_task({"task": "defang", "queue_id": "q2"}, hands.build(), NOW) == "approved"
    assert "replace_body" not in hands.names()


def test_a_cut_leaving_too_little_is_rejected_a_misquoted_one_waits(aws):
    put_topic(aws)
    put_held(aws, "q1", "a1", created_at=LONG_AGO, reasons=["PII: everything"])
    most = [
        "The Wow Forever patch lands this week.",
        CUT,
        "Players can expect shorter queues and a calmer economy all season.",
    ]
    hands = DefangHands([defang_answer(remove=most)])
    assert reviewer.do_task({"task": "defang", "queue_id": "q1"}, hands.build(), NOW) == "defang_rejected"

    put_held(aws, "q2", "a2", created_at=LONG_AGO, reasons=["PII: a person"])
    hands = DefangHands([defang_answer(remove=["A sentence that is not in the article."])])
    assert reviewer.do_task({"task": "defang", "queue_id": "q2"}, hands.build(), NOW) == "left_for_human"
    assert "reject" not in hands.names()


def test_task_five_never_publishes_where_he_may_not_and_a_fault_is_rejected(aws):
    put_topic(aws, "vision", force_manual_review=True)
    put_held(aws, "q1", "a1", topic_id="vision", created_at=LONG_AGO, reasons=["PII: x"])
    hands = DefangHands()
    assert reviewer.do_task({"task": "defang", "queue_id": "q1"}, hands.build(), NOW) == "left_for_human"
    assert hands.names() == []

    put_topic(aws)
    put_held(aws, "q2", "a2", created_at=LONG_AGO, reasons=["draft truncated: stopped"])
    hands = DefangHands()
    assert reviewer.do_task({"task": "defang", "queue_id": "q2"}, hands.build(), NOW) == "defang_rejected"


def test_a_financial_article_published_by_task_five_is_flagged_for_a_person(aws):
    put_topic(aws, "crypto", is_financial=True)
    put_held(aws, "q", "a", topic_id="crypto", created_at=LONG_AGO, reasons=["advice: says buy"])
    hands = DefangHands([defang_answer()])
    assert reviewer.do_task({"task": "defang", "queue_id": "q"}, hands.build(), NOW) == "defanged"
    assert article(aws, "a")[reviewer.FLAG_FIELD] == reviewer.FLAG_FINANCIAL


def test_cut_sentences_only_deletes():
    assert reviewer.cut_sentences(BODY, ["not there"]) is None
    cut = reviewer.cut_sentences(BODY, [CUT])
    assert all(line in BODY for line in cut.splitlines() if line)


# --- his settings -----------------------------------------------------------------------------------


def test_a_paused_reviewer_skips_his_scheduled_run(aws):
    dynamo.put_config_row({"config_id": reviewer.CONFIG_ID, "enabled": False})
    result = reviewer_handler.handle({"job": "run"}, "arn", NOW, hands=FakeHands().build())
    assert result == {"status": "paused"}


def test_his_limits_come_from_the_settings(aws, monkeypatch):
    put_topic(aws)
    put_held(aws, "q", "a", rewrites=[{"n": 1}])
    settings = {**reviewer.effective_settings(None), "max_rewrites": 1}
    hands = FakeHands()
    task = {"task": "held", "queue_id": "q"}
    assert reviewer.do_task(task, hands.build(), NOW, settings=settings) == "rejected"

    for n in range(4):
        put_held(aws, f"t{n}", f"b{n}", reasons=["draft truncated: x"])
    monkeypatch.setattr(reviewer_handler, "_report", lambda state: None)
    monkeypatch.setattr("ops_mcp.content.content_checks", lambda days, now: {"findings": []})
    monkeypatch.setattr(reviewer_handler, "_get_scheduler", lambda: FakeScheduler())
    dynamo.put_config_row({"config_id": reviewer.CONFIG_ID, "tasks_per_tick": 3, "tick_minutes": 12})
    first = reviewer_handler.handle({"job": "run"}, "arn", NOW, hands=FakeHands().build())
    assert first["left"] == 2  # five held (q is still pending: the fake reject changed nothing), three a tick
    assert reviewer.get_state()["next_tick_at"] == (NOW + timedelta(minutes=12)).isoformat()
