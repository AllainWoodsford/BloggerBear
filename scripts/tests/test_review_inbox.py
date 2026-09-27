"""Tests for scripts/review_inbox.py: the inbox and the approve loop."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import admin_cli
import review_inbox as ri

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeApi:
    """Plays the Admin API: routes map "METHOD /path" to a payload, an ApiError, or a callable."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict | None]] = []

    def _do(self, method: str, path: str):
        self.calls.append((method, path))
        result = self.routes.get(f"{method} {path}", {})
        if isinstance(result, Exception):
            raise result
        return result(path) if callable(result) else result

    def get(self, path):
        return self._do("GET", path)

    def post(self, path, body=None):
        self.bodies.append((path, body))
        return self._do("POST", path)


def keys(*pressed):
    """A key reader that plays back the keys in order (then quits)."""
    queue = list(pressed)
    return lambda: queue.pop(0) if queue else "q"


@pytest.fixture
def store(tmp_path):
    return ri.SkipStore(tmp_path / "skips.json", now=lambda: NOW)


def _run(sources, store, *pressed, **kwargs):
    out = io.StringIO()
    summary = ri.review(
        sources, store=store, key_reader=keys(*pressed), out=out, now=lambda: NOW, **kwargs
    )
    return summary, out.getvalue()


# --- the API wrapper ------------------------------------------------------------------


def test_api_returns_the_payload_on_success():
    api = ri.Api(lambda method, path, body=None: FakeResponse(200, {"items": [1]}))

    assert api.get("/x") == {"items": [1]}
    assert api.post("/x") == {"items": [1]}


@pytest.mark.parametrize(
    ("response", "status", "text"),
    [
        (FakeResponse(404, {"error": "not found"}), 404, "not found"),
        (FakeResponse(409, {"error": "is not pending"}), 409, "is not pending"),
        (FakeResponse(500, None), 500, "the API answered 500"),
        (FakeResponse(502, ["weird"]), 502, "the API answered 502"),
    ],
)
def test_api_turns_failures_into_api_errors(response, status, text):
    api = ri.Api(lambda method, path, body=None: response)

    with pytest.raises(ri.ApiError) as exc:
        api.get("/x")

    assert exc.value.status == status and text in exc.value.message


def test_a_403_says_what_to_check():
    api = ri.Api(lambda method, path, body=None: FakeResponse(403, None))

    with pytest.raises(ri.ApiError) as exc:
        api.get("/x")

    assert "allowlist" in exc.value.message and "credentials" in exc.value.message


@pytest.mark.parametrize("problem", [ConnectionError("down"), TimeoutError("slow"), OSError("gone")])
def test_a_dropped_connection_is_an_api_error_not_a_crash(problem):
    def request(method, path, body=None):
        raise problem

    with pytest.raises(ri.ApiError) as exc:
        ri.Api(request).get("/x")

    assert exc.value.status == 0 and "network problem" in exc.value.message


def test_a_success_with_a_non_object_body_is_an_empty_dict():
    api = ri.Api(lambda method, path, body=None: FakeResponse(200, ["x"]))

    assert api.get("/x") == {}


# --- articles waiting for you ---------------------------------------------------------


def _row(queue_id, created, reasons=None, notes=None, topic="finance-crypto-investing"):
    row = {
        "queue_id": queue_id,
        "article_id": f"art-{queue_id}",
        "topic_id": topic,
        "created_at": created,
        "reasons": reasons or ["financial topic - routed to manual moderation regardless of content"],
    }
    if notes:
        row["review_notes"] = notes
    return row


def _article(title="A Title", body="The text.", cost=0.0123, refs=2, **extra):
    return {
        "title": title,
        "body": body,
        "cost_aud": cost,
        "source_refs": [{"title": "s", "url": f"https://x/{n}"} for n in range(refs)],
        **extra,
    }


def _moderation(rows, articles=None):
    routes = {"GET /moderation-queue": {"items": rows}}
    for row in rows:
        routes[f"GET /articles/{row['article_id']}"] = (articles or {}).get(
            row["queue_id"], _article(title=f"Title {row['queue_id']}")
        )
    return FakeApi(routes)


def test_articles_come_oldest_first_and_carry_their_title_and_text():
    api = _moderation(
        [_row("new", "2026-09-21T10:00:00+00:00"), _row("old", "2026-09-20T10:00:00+00:00")]
    )

    items = ri.ModerationSource(api).fetch(10, set())

    assert [i.key for i in items] == ["old", "new"]
    assert items[0].title == "Title old" and items[0].body == "The text."
    assert "Sources cited: 2" in items[0].facts and "Cost to write: ~$0.012 AUD" in items[0].facts


def test_a_financial_only_article_is_routine_and_needs_no_extra_confirmation():
    (item,) = ri.ModerationSource(_moderation([_row("a", "2026-09-20T10:00:00+00:00")])).fetch(5, set())

    assert item.routine is True and item.caution is False
    assert item.why == ["Financial topic: always reviewed by a person."]


def test_an_article_held_for_a_reason_is_flagged_for_care():
    row = _row(
        "a",
        "2026-09-20T10:00:00+00:00",
        reasons=['Fabricated claim: "raised $50 million"'],
        notes=["Bitcoin is $79,400 now, not $81,000."],
    )

    (item,) = ri.ModerationSource(_moderation([row])).fetch(5, set())

    assert item.caution is True and item.routine is False
    assert item.why == ['Fabricated claim: "raised $50 million"']
    assert item.notes == ["Bitcoin is $79,400 now, not $81,000."]


def test_a_financial_article_with_review_notes_is_not_routine():
    row = _row("a", "2026-09-20T10:00:00+00:00", notes=["a claim looks stale"])

    (item,) = ri.ModerationSource(_moderation([row])).fetch(5, set())

    assert item.routine is False and item.caution is True


def test_skipped_articles_are_left_out_and_the_batch_is_limited():
    rows = [_row(f"q{n:02d}", f"2026-09-{10 + n}T10:00:00+00:00") for n in range(10)]

    items = ri.ModerationSource(_moderation(rows)).fetch(3, {"moderation:q00", "moderation:q01"})

    assert [i.key for i in items] == ["q02", "q03", "q04"]


def test_only_the_wanted_articles_are_loaded():
    rows = [_row(f"q{n}", f"2026-09-1{n}T10:00:00+00:00") for n in range(5)]
    api = _moderation(rows)

    ri.ModerationSource(api).fetch(2, set())

    assert [c for c in api.calls if c[1].startswith("/articles/")] == [
        ("GET", "/articles/art-q0"),
        ("GET", "/articles/art-q1"),
    ]


def test_an_article_that_cannot_be_loaded_is_still_listed_with_a_warning():
    rows = [_row("a", "2026-09-20T10:00:00+00:00")]
    api = FakeApi(
        {"GET /moderation-queue": {"items": rows}, "GET /articles/art-a": ri.ApiError(500, "boom")}
    )

    (item,) = ri.ModerationSource(api).fetch(5, set())

    assert item.title == "(article unavailable)" and item.caution is True
    assert any("Could not load the article text: boom" in n for n in item.notes)


def test_an_article_whose_text_could_not_be_read_asks_for_care():
    api = _moderation(
        [_row("a", "2026-09-20T10:00:00+00:00")],
        {"a": _article(body="", body_error="could not read the article text (KeyError)")},
    )

    (item,) = ri.ModerationSource(api).fetch(5, set())

    assert item.caution is True and "could not read the article text" in item.notes[0]


def test_an_article_with_no_cost_data_says_so():
    api = _moderation([_row("a", "2026-09-20T10:00:00+00:00")], {"a": _article(cost=None)})

    (item,) = ri.ModerationSource(api).fetch(5, set())

    assert "Cost: no data" in item.facts


def test_approving_and_rejecting_an_article_call_the_moderation_routes():
    api = _moderation([_row("q1", "2026-09-20T10:00:00+00:00")])
    source = ri.ModerationSource(api)
    (item,) = source.fetch(5, set())

    assert source.approve(item) == "published"
    assert "rejected" in source.reject(item)
    assert ("POST", "/moderation-queue/q1/approve") in api.calls
    assert ("POST", "/moderation-queue/q1/reject") in api.calls


# --- prompt changes -------------------------------------------------------------------


def _refinement(topic, version, rationale="Readers wanted charts", changes="Add a chart."):
    return {
        "topic_id": topic,
        "version": version,
        "rationale": rationale,
        "prompt_changes": changes,
        "status": "pending",
    }


def test_prompt_changes_show_why_and_what_and_warn_that_approving_changes_future_drafts():
    api = FakeApi(
        {
            "GET /prompt-refinements?status=pending": {
                "refinements": [
                    _refinement("t2", "2026-09-21T00:00:00+00:00"),
                    _refinement("t1", "2026-09-14T00:00:00+00:00", changes="Use tables."),
                ]
            }
        }
    )

    items = ri.RefinementSource(api).fetch(10, set())

    assert [i.topic for i in items] == ["t1", "t2"]  # oldest first
    first = items[0]
    assert first.title == "Prompt change for t1" and first.body == "Use tables."
    assert first.why == ["Readers wanted charts"]
    assert "future articles" in first.approve_note


def test_prompt_changes_skip_hidden_ones_and_respect_the_limit():
    rows = [_refinement("t", f"2026-09-1{n}T00:00:00+00:00") for n in range(5)]
    api = FakeApi({"GET /prompt-refinements?status=pending": {"refinements": rows}})

    items = ri.RefinementSource(api).fetch(2, {"refinements:t|2026-09-10T00:00:00+00:00"})

    assert [i.created_at for i in items] == ["2026-09-11T00:00:00+00:00", "2026-09-12T00:00:00+00:00"]


def test_approving_and_rejecting_a_prompt_change_call_the_refinement_routes():
    api = FakeApi({"GET /prompt-refinements?status=pending": {"refinements": [_refinement("t1", "v1")]}})
    source = ri.RefinementSource(api)
    (item,) = source.fetch(5, set())

    source.approve(item)
    source.reject(item)

    assert ("POST", "/prompt-refinements/t1/v1/approve") in api.calls
    assert ("POST", "/prompt-refinements/t1/v1/reject") in api.calls


# --- where an approved prompt change is worn -------------------------------------------

ENTER = chr(13)
ARMOR = ("helmet", "chest", "gloves", "boots", "sword", "shield")


def _loadout(worn_armor=None, rings=0, backpack=0):
    """What GET /equipment answers: `worn_armor` maps a slot to the text worn there."""
    worn_armor = worn_armor or {}
    return {
        "armor": {
            slot: {"topic_id": "x", "version": f"v-{slot}", "prompt_changes": worn_armor[slot]}
            if slot in worn_armor
            else None
            for slot in ARMOR
        },
        "rings": [
            {"topic_id": f"t{n}", "version": f"r{n}", "prompt_changes": f"Ring {n} guidance."}
            for n in range(rings)
        ],
        "max_rings": 5,
        "backpack_count": backpack,
    }


def _prompt_change_api(loadout=None, approve_response=None, row=None):
    return FakeApi(
        {
            "GET /prompt-refinements?status=pending": {"refinements": [row or _refinement("t1", "v1")]},
            "GET /equipment": loadout if loadout is not None else _loadout(),
            "POST /prompt-refinements/t1/v1/approve": approve_response or {},
        }
    )


def _approve_with(api, *pressed):
    """Press y on the one waiting prompt change, then `pressed` to answer the placement questions."""
    out = io.StringIO()
    summary = ri.review(
        [ri.RefinementSource(api)],
        store=_memory_store(),
        key_reader=keys("y", *pressed),
        out=out,
        now=lambda: NOW,
    )
    return summary, out.getvalue()


def _memory_store():
    return ri.SkipStore(Path(tempfile.mkdtemp()) / "skips.json", now=lambda: NOW)


def test_approving_a_prompt_change_asks_where_the_bear_wears_it_and_defaults_to_a_ring():
    api = _prompt_change_api(approve_response={"approved": {"placement": {"equipped": True, "slot": "ring"}}})

    summary, out = _approve_with(api, ENTER)

    assert "Where should the bear wear it?" in out
    assert "a ring for t1 (0 of 5 rings worn)" in out
    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "topic"})]
    assert summary.approved == 1
    assert "worn as a ring for its topic" in out


def test_a_prompt_change_can_go_to_the_backpack():
    api = _prompt_change_api(
        loadout=_loadout(backpack=3), approve_response={"approved": {"placement": {"equipped": False}}}
    )

    summary, out = _approve_with(api, "b")

    assert "3 there now" in out
    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "backpack"})]
    assert "in the backpack, not worn" in out
    assert summary.approved == 1


def test_a_global_prompt_change_defaults_to_the_first_empty_armor_slot_and_names_what_is_worn():
    api = _prompt_change_api(loadout=_loadout({"helmet": "Keep it short.", "chest": "Cite sources."}))

    _, out = _approve_with(api, "g", ENTER)

    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "global", "slot": "gloves"})]
    assert "helmet: worn: Keep it short." in out and "gloves: empty" in out


def test_picking_an_occupied_armor_slot_says_what_it_would_replace():
    api = _prompt_change_api(loadout=_loadout({slot: f"Guidance for {slot}." for slot in ARMOR}))

    _, out = _approve_with(api, "g", "5")

    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "global", "slot": "sword"})]
    assert "sword: worn: Guidance for sword.  <- it would be replaced" in out
    assert "(all are worn)" in out


def test_with_every_ring_worn_it_asks_which_to_replace():
    api = _prompt_change_api(loadout=_loadout(rings=5))

    _, out = _approve_with(api, "t", "3")

    assert "Every ring is worn. Replace which one?" in out and "2  t1: Ring 1 guidance." in out
    assert api.bodies == [
        (
            "/prompt-refinements/t1/v1/approve",
            {"scope": "topic", "replace": {"topic_id": "t2", "version": "r2"}},
        )
    ]


def test_cancelling_the_placement_leaves_the_prompt_change_untouched():
    api = _prompt_change_api()

    summary, _ = _approve_with(api, "c")  # then the loop's own keys run out, which quits

    assert api.bodies == []
    assert summary.approved == 0 and summary.quit_early


def test_an_unknown_key_at_the_placement_question_asks_again():
    api = _prompt_change_api()

    _, out = _approve_with(api, "x", "b")

    assert "Press one of: t, g, b, or c to cancel." in out
    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "backpack"})]


def test_if_the_loadout_cannot_be_read_you_stay_on_the_item():
    api = _prompt_change_api(loadout=ri.ApiError(500, "boom"))

    summary, out = _approve_with(api)

    assert "Could not read what the bear is wearing: boom" in out
    assert api.bodies == [] and summary.approved == 0


def test_a_dry_run_does_not_ask_where_it_would_be_worn():
    api = _prompt_change_api()

    out = io.StringIO()
    ri.review(
        [ri.RefinementSource(api)],
        store=_memory_store(),
        key_reader=keys("y"),
        out=out,
        now=lambda: NOW,
        dry_run=True,
    )

    assert "Where should the bear wear it?" not in out.getvalue()
    assert ("GET", "/equipment") not in api.calls and api.bodies == []


def test_approving_an_article_never_asks_about_gear():
    source = ri.MockSource(count=1)

    out = io.StringIO()
    ri.review([source], store=_memory_store(), key_reader=keys("y"), out=out, now=lambda: NOW)

    assert "Where should the bear wear it?" not in out.getvalue()


def _found(slot_hint, rarity="rare"):
    """A pending proposal the weekly reflection has already named."""
    return {
        **_refinement("t1", "v1"),
        "name": "Breastplate of Plain Speaking",
        "rarity": rarity,
        "durability": 17,
        "max_durability": 17,
        "slot_hint": slot_hint,
    }


def test_a_named_proposal_shows_what_the_bear_found():
    api = _prompt_change_api(row=_found("chest"))

    (item,) = ri.RefinementSource(api).fetch(5, set())

    assert item.facts == [
        "The bear found: Breastplate of Plain Speaking (rare)",
        "Durability: 17/17",
        "The bear suggests: chest",
    ]
    assert item.ref["slot_hint"] == "chest"


def test_a_proposal_from_before_gear_shows_no_gear_facts():
    (item,) = ri.RefinementSource(_prompt_change_api()).fetch(5, set())

    assert item.facts == []


def test_when_the_bear_suggests_armor_enter_takes_its_slot():
    api = _prompt_change_api(loadout=_loadout({"helmet": "Keep it short."}), row=_found("shield"))

    _, out = _approve_with(api, ENTER, ENTER)  # accept the armor suggestion, then its slot

    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "global", "slot": "shield"})]
    assert "the bear suggests the shield  [Enter]" in out
    assert "shield: empty  *" in out


def test_when_the_suggested_slot_is_taken_enter_offers_the_first_empty_one_instead():
    api = _prompt_change_api(loadout=_loadout({"shield": "Cite sources."}), row=_found("shield"))

    _, out = _approve_with(api, "g", ENTER)

    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "global", "slot": "helmet"})]
    assert "shield: worn: Cite sources.  <- it would be replaced" in out
    assert "helmet: empty  *" in out


def test_when_the_bear_suggests_a_ring_enter_takes_a_ring():
    api = _prompt_change_api(row=_found("ring"))

    _, out = _approve_with(api, ENTER)

    assert api.bodies == [("/prompt-refinements/t1/v1/approve", {"scope": "topic"})]
    ring_line = next(line for line in out.splitlines() if "t  a ring" in line)
    assert ring_line.endswith("[Enter]")


def test_approving_tells_you_what_the_bear_found_and_where_it_went():
    response = {
        "approved": {
            "item": {
                "name": "Ring of Plain Speaking",
                "rarity": "epic",
                "durability": 24,
                "max_durability": 24,
            },
            "placement": {"equipped": True, "slot": "ring", "scope": "topic", "displaced": None},
        }
    }
    api = _prompt_change_api(approve_response=response)

    _, out = _approve_with(api, "t")

    assert "Ring of Plain Speaking (epic, durability 24/24): worn as a ring for its topic" in out


def test_the_worn_message_tells_you_what_replaced_what():
    placement = {
        "equipped": True,
        "slot": "helmet",
        "scope": "global",
        "displaced": {"topic_id": "t9", "version": "v9"},
    }

    assert ri._worn_message(placement) == (
        "worn as global guidance in the helmet slot (replacing t9 v9); future drafts will use it"
    )


# --- remembering what you skipped -----------------------------------------------------


def test_skips_are_remembered_between_runs_in_a_local_file(tmp_path):
    path = tmp_path / "skips.json"
    ri.SkipStore(path, now=lambda: NOW).add("moderation:a")

    again = ri.SkipStore(path, now=lambda: NOW)

    assert again.active(24) == {"moderation:a"}
    assert json.loads(path.read_text())["skipped"].keys() == {"moderation:a"}


def test_skips_lapse_after_the_chosen_hours():
    store = ri.SkipStore("unused.json", now=lambda: NOW)
    store._skips = {
        "a": (NOW - timedelta(hours=2)).isoformat(),
        "b": (NOW - timedelta(hours=30)).isoformat(),
        "c": "not a date",
        "d": (NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat(),  # no zone: read as UTC
    }

    assert store.active(24) == {"a", "d"}
    assert store.active(0) == {"a", "b", "c", "d"}  # 0 = until cleared


def test_a_decided_item_is_forgotten_and_clear_forgets_everything(tmp_path):
    store = ri.SkipStore(tmp_path / "s.json", now=lambda: NOW)
    store.add("a")
    store.add("b")

    store.forget("a")
    assert store.active(0) == {"b"}
    assert store.clear() == 1 and store.active(0) == set()


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"skipped": "oops"}', '{"skipped": {"a": 1}}'])
def test_a_damaged_skip_file_just_starts_empty(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content)

    store = ri.SkipStore(path, now=lambda: NOW)

    assert store.active(24) <= {"a"}  # nothing crashes; at worst a coerced entry


def test_a_skip_file_that_cannot_be_written_warns_and_carries_on(tmp_path, capsys):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder")
    store = ri.SkipStore(blocker / "skips.json", now=lambda: NOW)

    store.add("moderation:a")  # must not raise

    assert "could not remember your skips" in capsys.readouterr().err
    assert store.active(24) == {"moderation:a"}  # still remembered for this run


def test_the_state_file_location_can_be_set(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOGGERBEAR_REVIEW_STATE", str(tmp_path / "mine.json"))

    assert ri.SkipStore().path == tmp_path / "mine.json"


# --- what a card looks like -----------------------------------------------------------


def _item(**overrides):
    fields = {
        "source": "moderation",
        "key": "k",
        "title": "A Very Interesting Article",
        "topic": "github-trending",
        "created_at": (NOW - timedelta(hours=5)).isoformat(),
        "why": ["Fabricated claim: something"],
        "notes": ["A note worth reading"],
        "facts": ["Sources cited: 3"],
        "body": "Some body text.",
    }
    return ri.Item(**{**fields, **overrides})


def test_a_card_shows_where_it_is_from_what_it_is_why_it_needs_you_and_a_preview():
    card = ri.render_item(_item(), 2, 7, NOW, width=80)

    assert "[2/7] ARTICLE" in card
    assert "A Very Interesting Article" in card
    assert "Topic: github-trending" in card and "5 h ago" in card and "2026-09-21 07:00 UTC" in card
    assert "Sources cited: 3" in card
    assert "Why it needs you:" in card and "- Fabricated claim: something" in card
    assert "Review notes (look before approving):" in card and "! A Very" not in card
    assert "! A note worth reading" in card
    assert "Some body text." in card


@pytest.mark.parametrize(
    ("source", "label"),
    [("moderation", "ARTICLE"), ("refinements", "PROMPT CHANGE"), ("mock", "PRACTICE"), ("x", "X")],
)
def test_each_kind_of_item_has_a_plain_label(source, label):
    assert _item(source=source).kind == label


def test_a_long_text_is_cut_with_a_hint_to_read_the_rest():
    card = ri.render_item(_item(body="word " * 400), 1, 1, NOW, width=80)

    assert "more characters: press v to read all of it" in card
    assert card.count("word") < 400


def test_a_card_with_no_text_says_so():
    assert "(no text to preview)" in ri.render_item(_item(body=""), 1, 1, NOW, width=80)


def test_the_note_about_what_approving_does_is_shown():
    card = ri.render_item(_item(approve_note="Approving changes future drafts."), 1, 1, NOW, width=80)

    assert "Note: Approving changes future drafts." in card


def test_age_reads_naturally():
    assert ri._age((NOW - timedelta(minutes=10)).isoformat(), NOW).startswith("10 min ago")
    assert ri._age((NOW - timedelta(days=3)).isoformat(), NOW).startswith("3 d ago")
    assert ri._age(None, NOW) == "unknown age"
    assert ri._age("garbage", NOW) == "garbage"
    assert ri._age((NOW + timedelta(hours=1)).isoformat(), NOW).startswith("1 min ago")


# --- the loop -------------------------------------------------------------------------


class ListSource(ri.ContentSource):
    """A source over a plain list, recording what was done to it."""

    name = "test"
    label = "test items"

    def __init__(self, count=5, fail_on=None, already_done=None):
        self.items = [
            ri.Item(source="test", key=f"i{n:02d}", title=f"Item {n}", body=f"text {n}")
            for n in range(count)
        ]
        self.done: list[tuple[str, str]] = []
        self.fail_on = fail_on or {}
        self.already_done = already_done or set()

    def fetch(self, limit, exclude):
        return [i for i in self.items if i.skip_key not in exclude][:limit]

    def _act(self, verb, item):
        if item.key in self.fail_on and self.fail_on[item.key] > 0:
            self.fail_on[item.key] -= 1
            raise ri.ApiError(500, "the server had a wobble")
        if item.key in self.already_done:
            raise ri.ApiError(409, "not pending")
        self.done.append((verb, item.key))
        return f"{verb} ok"

    def approve(self, item):
        return self._act("approve", item)

    def reject(self, item):
        return self._act("reject", item)


def test_y_r_z_apply_at_once_and_the_summary_counts_them(store):
    source = ListSource(3)

    summary, out = _run([source], store, "y", "r", "z")

    assert source.done == [("approve", "i00"), ("reject", "i01")]
    assert (summary.approved, summary.rejected, summary.skipped) == (1, 1, 1)
    assert "1 approved, 1 rejected, 1 skipped." in out
    assert store.active(24) == {"test:i02"}  # the skipped one is remembered


def test_a_skip_changes_nothing_at_the_source(store):
    source = ListSource(1)

    _run([source], store, "z")

    assert source.done == []


def test_v_shows_the_whole_text_then_asks_again(store):
    source = ListSource(1)
    source.items[0].body = "the full story " * 100

    summary, out = _run([source], store, "v", "y")

    assert out.count("the full story") > 100  # printed in full, not just the preview
    assert summary.approved == 1


def test_q_stops_and_keeps_what_was_already_decided(store):
    source = ListSource(4)

    summary, out = _run([source], store, "y", "q")

    assert summary.quit_early and source.done == [("approve", "i00")]
    assert "nothing you already decided is lost" in out
    assert "1 approved, 0 rejected, 0 skipped." in out


def test_ctrl_c_is_a_quit_not_a_crash(store):
    def reader():
        raise KeyboardInterrupt

    out = io.StringIO()
    summary = ri.review([ListSource(2)], store=store, key_reader=reader, out=out, now=lambda: NOW)

    assert summary.quit_early is True


def test_an_unknown_key_asks_again(store):
    source = ListSource(1)

    summary, out = _run([source], store, "x", "?", "", "y")

    assert out.count("Press y, r, z, v or q.") == 3
    assert summary.approved == 1


def test_at_most_thirty_at_a_time_and_a_rerun_gives_the_next_batch(store):
    source = ListSource(45)

    first, out = _run([source], store, *(["z"] * 30))
    second, _ = _run([source], store, *(["z"] * 15))

    assert first.fetched == 30 and first.skipped == 30
    assert "run it again for the next batch" in out
    # The 30 skipped ones are hidden, so the rerun moves on to the other 15.
    assert second.fetched == 15
    assert store.active(24) == {f"test:i{n:02d}" for n in range(45)}


def test_skipped_items_can_be_shown_again_on_request(store):
    source = ListSource(3)
    _run([source], store, "z", "z", "z")

    hidden, out = _run([source], store)
    shown, _ = _run([source], store, include_skipped=True)

    assert hidden.fetched == 0 and "Skipped items are hidden" in out
    assert shown.fetched == 3


def test_a_skip_lapses_so_the_item_comes_back(tmp_path):
    later = [NOW]
    store = ri.SkipStore(tmp_path / "s.json", now=lambda: later[0])
    source = ListSource(1)
    _run([source], store, "z")

    later[0] = NOW + timedelta(hours=25)

    assert ri.review([source], store=store, key_reader=keys("q"), out=io.StringIO()).fetched == 1


def test_an_item_you_decide_on_is_forgotten_from_the_skip_list(store):
    source = ListSource(1)
    _run([source], store, "z")
    _run([source], store, "y", include_skipped=True)

    assert store.active(0) == set()


def test_an_item_held_for_a_reason_needs_a_second_yes(store):
    source = ListSource(1)
    source.items[0].caution = True

    declined, out = _run([source], store, "y", "n", "z")
    approved, _ = _run([source], store, "y", "y", include_skipped=True)

    assert "Approve anyway? [y/N]" in out
    assert declined.approved == 0 and source.done == [("approve", "i00")]
    assert approved.approved == 1


def test_rejecting_a_held_item_needs_no_extra_confirmation(store):
    source = ListSource(1)
    source.items[0].caution = True

    summary, out = _run([source], store, "r")

    assert summary.rejected == 1 and "Approve anyway" not in out


def test_a_failure_keeps_you_on_the_item_so_you_can_try_again(store):
    source = ListSource(2, fail_on={"i00": 1})

    summary, out = _run([source], store, "y", "y", "y")

    assert "Could not approve this one: the server had a wobble" in out
    assert summary.errors == 1 and summary.approved == 2  # the retry worked, then the next one
    assert source.done == [("approve", "i00"), ("approve", "i01")]


def test_after_a_failure_you_can_move_on_without_losing_the_rest(store):
    source = ListSource(3, fail_on={"i00": 99})

    summary, out = _run([source], store, "y", "z", "y", "y")

    assert summary.errors == 1 and summary.skipped == 1 and summary.approved == 2
    assert "1 failed" in out


def test_something_already_handled_elsewhere_is_noted_and_moved_past(store):
    source = ListSource(2, already_done={"i00"})

    summary, out = _run([source], store, "y", "y")

    assert "Already handled elsewhere" in out
    assert summary.already_handled == 1 and summary.approved == 1
    assert "1 already handled elsewhere" in out


def test_a_dry_run_changes_nothing_and_remembers_nothing(store):
    source = ListSource(3)

    summary, out = _run([source], store, "y", "r", "z", dry_run=True)

    assert source.done == [] and store.active(0) == set()
    assert "(dry run) would approve." in out and "(dry run) would reject." in out
    assert "dry run: nothing was changed" in out and summary.dry_run


def test_nothing_waiting_says_so(store):
    summary, out = _run([ListSource(0)], store)

    assert summary.fetched == 0 and "Nothing is waiting for you." in out


def test_the_batch_is_shared_across_sources_and_articles_come_first(store):
    first, second = ListSource(4), ListSource(4)
    second.name = "other"
    for item in second.items:
        item.source = "other"

    summary, out = _run([first, second], store, *(["z"] * 5), limit=5)

    assert summary.fetched == 5
    assert [i.source for i in first.items[:4]] and store.active(0) >= {"test:i00", "other:i00"}


def test_a_source_that_cannot_be_reached_is_reported_and_the_rest_still_work(store):
    class Broken(ListSource):
        label = "broken things"

        def fetch(self, limit, exclude):
            raise ri.ApiError(0, "network problem (ConnectionError)")

    good = ListSource(1)

    summary, out = _run([Broken(), good], store, "y")

    assert "Could not fetch broken things: network problem" in out
    assert summary.approved == 1


def test_a_card_with_unprintable_characters_does_not_crash_a_narrow_console(store):
    class Cp1252Out(io.StringIO):
        encoding = "cp1252"

        def write(self, text):
            text.encode("cp1252")  # like a Windows console: raises for characters it cannot show
            return super().write(text)

    source = ListSource(1)
    source.items[0].title = "Bitcoin → $100k \U0001f43b"
    out = Cp1252Out()

    summary = ri.review([source], store=store, key_reader=keys("y"), out=out, now=lambda: NOW)

    assert summary.approved == 1
    assert "Bitcoin" in out.getvalue()


# --- reading a key --------------------------------------------------------------------


def test_without_a_terminal_a_line_of_input_is_read_and_its_first_letter_used(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("Yes please\nr\n"))

    assert ri.read_key() == "y"
    assert ri.read_key() == "r"


def test_end_of_input_counts_as_quit(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    assert ri.read_key() == "q"


def test_a_blank_line_is_no_key(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))

    assert ri.read_key() == ""


# --- practice mode --------------------------------------------------------------------


def test_practice_items_need_no_aws_and_record_what_you_did(store):
    source = ri.MockSource(count=6, now=NOW)

    summary, out = _run([source], store, "y", "r", "y", "y", "z", "q")

    assert summary.fetched == 6
    assert ("approve", "mock-1") in source.log and ("reject", "mock-2") in source.log
    assert "practice: nothing was sent" in out


def test_practice_items_include_routine_and_held_ones_so_you_can_see_both():
    items = ri.MockSource(count=4, now=NOW).fetch(10, set())

    assert any(i.routine for i in items) and any(i.caution for i in items)
    assert all(i.title.endswith(")") for i in items)


def test_build_sources_picks_by_name():
    api = FakeApi({})

    assert [s.name for s in ri.build_sources(api, "all", False)] == ["moderation", "refinements"]
    assert [s.name for s in ri.build_sources(api, "moderation", False)] == ["moderation"]
    assert [s.name for s in ri.build_sources(api, "refinements", False)] == ["refinements"]
    assert [s.name for s in ri.build_sources(None, "all", True)] == ["mock"]
    with pytest.raises(ValueError):
        ri.build_sources(None, "all", False)


# --- the inbox summary ----------------------------------------------------------------


def test_the_inbox_says_what_is_waiting_and_what_to_look_at_first():
    api = FakeApi(
        {
            "GET /moderation-queue": {
                "items": [
                    _row("a", "2026-09-19T10:00:00+00:00"),
                    _row("b", "2026-09-20T10:00:00+00:00"),
                    _row("c", "2026-09-20T11:00:00+00:00", reasons=["Investment advice: buy it"]),
                ]
            },
            "GET /prompt-refinements?status=pending": {"refinements": [_refinement("t", "v")]},
            "GET /failed-executions": {"items": [{"x": 1}]},
            "GET /feedback-config": {"effective": {"locked_down": True, "verification_required": False}},
        }
    )

    report = ri.inbox_report(api, NOW)

    assert "Articles waiting for you: 3" in report
    assert "2 on financial topics" in report and "1 held by a review" in report
    assert "oldest: 2 d ago" in report
    assert "Prompt changes waiting for you: 1" in report
    assert "Failed runs (dead-letter queue): 1" in report
    assert "feedback is LOCKED DOWN" in report and "verification is switched OFF" in report
    assert "admin_cli.py approve" in report


def test_an_empty_inbox_is_calm():
    api = FakeApi(
        {
            "GET /moderation-queue": {"items": []},
            "GET /prompt-refinements?status=pending": {"refinements": []},
            "GET /failed-executions": {"items": []},
            "GET /feedback-config": {"effective": {}},
        }
    )

    report = ri.inbox_report(api, NOW)

    assert "Articles waiting for you: 0" in report and "Heads-up" not in report


def test_a_part_of_the_inbox_that_cannot_be_checked_says_so_and_the_rest_still_shows():
    api = FakeApi(
        {
            "GET /moderation-queue": ri.ApiError(0, "network problem (ConnectionError)"),
            "GET /prompt-refinements?status=pending": {"refinements": []},
            "GET /failed-executions": ri.ApiError(500, "boom"),
            "GET /feedback-config": ri.ApiError(500, "boom"),
        }
    )

    report = ri.inbox_report(api, NOW)

    assert "Articles waiting for you: could not check (network problem" in report
    assert "Prompt changes waiting for you: 0" in report
    assert "Failed runs: could not check (boom)" in report


# --- wired into admin_cli -------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOGGERBEAR_REVIEW_STATE", str(tmp_path / "skips.json"))
    monkeypatch.delenv("BLOGGERBEAR_REVIEW_MOCK", raising=False)
    monkeypatch.delenv("BLOGGERBEAR_ADMIN_API_URL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    return tmp_path


def test_approve_in_practice_mode_needs_no_credentials_and_uses_its_own_skip_file(
    cli_env, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "stdin", io.StringIO("z\nq\n"))

    admin_cli.main(["approve", "--mock", "--limit", "3"])

    output = capsys.readouterr().out
    assert "PRACTICE MODE" in output and "made-up items" in output
    assert (cli_env / "review-skips-practice.json").exists()  # never the real skip file
    assert not (cli_env / "skips.json").exists()


def test_the_practice_toggle_can_be_an_environment_variable(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("BLOGGERBEAR_REVIEW_MOCK", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("q\n"))

    admin_cli.main(["approve"])

    assert "PRACTICE MODE" in capsys.readouterr().out


def test_inbox_in_practice_mode_prints_made_up_numbers(cli_env, capsys):
    admin_cli.main(["inbox", "--mock"])

    assert "practice: made-up numbers" in capsys.readouterr().out


def test_a_live_approve_goes_through_the_signed_admin_api(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    calls = []

    def fake_signed(method, api_url, path, region, body=None):
        calls.append((method, path))
        if path == "/moderation-queue":
            return FakeResponse(200, {"items": [_row("q1", "2026-09-20T10:00:00+00:00")]})
        if path.startswith("/articles/"):
            return FakeResponse(200, _article())
        if path.startswith("/prompt-refinements?"):
            return FakeResponse(200, {"refinements": []})
        return FakeResponse(200, {"approved": "q1"})

    with patch("admin_cli.signed_request", side_effect=fake_signed):
        admin_cli.main(
            ["--api-url", "https://api.example.com", "--region", "ap-southeast-2", "approve"]
        )

    assert ("POST", "/moderation-queue/q1/approve") in calls
    assert "1 approved, 0 rejected, 0 skipped." in capsys.readouterr().out


def test_a_live_inbox_reads_from_the_admin_api(cli_env, capsys):
    def fake_signed(method, api_url, path, region, body=None):
        return FakeResponse(200, {"items": [], "refinements": [], "effective": {}})

    with patch("admin_cli.signed_request", side_effect=fake_signed):
        admin_cli.main(
            ["--api-url", "https://api.example.com", "--region", "ap-southeast-2", "inbox"]
        )

    assert "Articles waiting for you: 0" in capsys.readouterr().out


def test_a_live_run_without_configuration_is_refused_clearly(cli_env, capsys):
    with pytest.raises(SystemExit):
        admin_cli.main(["approve"])

    assert "Admin API URL not configured" in capsys.readouterr().err


def test_limit_must_be_positive(cli_env, capsys):
    with pytest.raises(SystemExit):
        admin_cli.main(["approve", "--mock", "--limit", "0"])

    assert "--limit must be at least 1" in capsys.readouterr().err


def test_approving_a_prompt_change_says_when_a_loot_drop_was_posted():
    response = {
        "approved": {
            "item": {
                "name": "Ring of Plain Speaking",
                "rarity": "epic",
                "durability": 24,
                "max_durability": 24,
            },
            "placement": {"equipped": True, "slot": "ring", "scope": "topic", "displaced": None},
            "loot_drop": "m-1",
        }
    }
    api = _prompt_change_api(approve_response=response)

    _, out = _approve_with(api, "t")

    assert "A loot drop was posted to the Musings." in out


def test_no_loot_drop_line_when_none_was_posted():
    response = {"approved": {"placement": {"equipped": True, "slot": "ring"}, "loot_drop": None}}

    _, out = _approve_with(_prompt_change_api(approve_response=response), "t")

    assert "loot drop" not in out.lower()


# --- Re-Write ([w]) ---------------------------------------------------------------------------

MODELS = {
    "models": [
        {"model_id": "haiku", "display_name": "Claude Haiku", "enabled": True,
         "input_price_usd_per_1k_tokens": 0.001, "output_price_usd_per_1k_tokens": 0.005},
        {"model_id": "old", "display_name": "Retired", "enabled": False},
        {"model_id": "sonnet", "display_name": "Claude Sonnet"},
    ]
}


def _held(queue_id="q1", **extra):
    return {**_row(queue_id, "2026-09-20T10:00:00+00:00", reasons=["Investment advice: buy it"]), **extra}


def _rewrite_api(rows, rewrite_result=None):
    api = _moderation(rows)
    api.routes["GET /models"] = MODELS
    for row in rows:
        api.routes[f"POST /moderation-queue/{row['queue_id']}/rewrite"] = (
            rewrite_result if rewrite_result is not None else {"rewriting": row["queue_id"]}
        )
    return api


def test_only_an_article_held_for_a_reason_can_be_rewritten():
    api = _moderation([_held("held"), _row("routine", "2026-09-21T10:00:00+00:00")])

    held, routine = ri.ModerationSource(api).fetch(5, set())

    assert held.can_rewrite is True and routine.can_rewrite is False


def test_w_asks_for_a_model_starts_the_rewrite_and_moves_on(store):
    api = _rewrite_api([_held("q1"), _held("q2")])

    summary, out = _run([ri.ModerationSource(api)], store, "w", "2", "z")

    assert api.bodies[0] == ("/moderation-queue/q1/rewrite", {"model_id": "sonnet"})
    assert summary.rewritten == 1 and summary.skipped == 1
    assert "[w] Re-Write" in out and "[1] Claude Haiku" in out and "Retired" not in out
    assert "back in your inbox" in out and "1 sent for a Re-Write" in out


def test_cancelling_the_model_choice_stays_on_the_item(store):
    api = _rewrite_api([_held("q1")])

    summary, _ = _run([ri.ModerationSource(api)], store, "w", "c", "r")

    assert summary.rewritten == 0 and summary.rejected == 1
    assert not any(path.endswith("/rewrite") for path, _ in api.bodies)


def test_w_does_nothing_on_a_routine_article(store):
    api = _rewrite_api([_row("q1", "2026-09-20T10:00:00+00:00")])

    summary, out = _run([ri.ModerationSource(api)], store, "w", "z")

    assert summary.rewritten == 0 and "[w] Re-Write" not in out
    assert ("GET", "/models") not in api.calls


def test_a_rewrite_already_handled_elsewhere_moves_on(store):
    api = _rewrite_api([_held("q1")], rewrite_result=ri.ApiError(409, "not pending"))

    summary, out = _run([ri.ModerationSource(api)], store, "w", "1")

    assert summary.already_handled == 1 and "Already handled elsewhere" in out


def test_a_rewrite_that_fails_to_start_stays_on_the_item(store):
    api = _rewrite_api([_held("q1")], rewrite_result=ri.ApiError(502, "could not start"))

    summary, out = _run([ri.ModerationSource(api)], store, "w", "1", "z")

    assert summary.errors == 1 and summary.skipped == 1 and "Could not start a Re-Write" in out


def test_a_dry_run_rewrite_sends_nothing(store):
    api = _rewrite_api([_held("q1")])

    summary, out = _run([ri.ModerationSource(api)], store, "w", "1", dry_run=True)

    assert summary.rewritten == 1 and "(dry run) would rewrite with haiku" in out
    assert not api.bodies


def test_with_no_enabled_models_there_is_nothing_to_choose(store):
    api = _rewrite_api([_held("q1")])
    api.routes["GET /models"] = {"models": [{"model_id": "old", "enabled": False}]}

    summary, out = _run([ri.ModerationSource(api)], store, "w", "z")

    assert summary.rewritten == 0 and "No enabled models" in out


def test_a_rewritten_article_says_which_rewrite_by_what_and_for_how_much():
    rewrite = {"number": 2, "model_label": "Claude Haiku", "cost_aud": 0.0042, "previous_title": "Buy Now"}
    (item,) = ri.ModerationSource(_moderation([_held("q1", rewrite=rewrite)])).fetch(5, set())

    assert 'Re-Write #2 by Claude Haiku (~$0.004 AUD); was titled "Buy Now"' in item.facts


def test_a_failed_rewrite_is_shown_on_the_original():
    row = _held("q1", last_rewrite_error="the rewrite could not be trusted: introduces figure(s)")
    (item,) = ri.ModerationSource(_moderation([row])).fetch(5, set())

    assert any("The last Re-Write did not work" in note for note in item.notes)


def test_the_practice_inbox_can_rewrite_too(store):
    source = ri.MockSource(count=3)

    summary, _ = _run([source], store, "z", "z", "w", "1")

    assert summary.rewritten == 1 and source.log == [("rewrite", "mock-3:practice-small")]


def test_the_inbox_mentions_rewrites_in_progress():
    api = FakeApi(
        {
            "GET /moderation-queue": {"items": [], "rewriting": 2},
            "GET /prompt-refinements?status=pending": {"refinements": []},
            "GET /failed-executions": {"items": []},
            "GET /feedback-config": {"effective": {}},
        }
    )

    assert "Being rewritten in the background: 2" in ri.inbox_report(api, NOW)
