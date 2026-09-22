"""Tests for scripts/gear_create.py: making and deleting gear by hand from the CLI."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

import admin_cli
import gear_create as gc
from review_inbox import ApiError


class FakeApi:
    """Plays the Admin API: routes map "METHOD /path" to a payload or an exception."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple] = []

    def _do(self, method, path, body=None):
        self.calls.append((method, path, body))
        result = self.routes.get(f"{method} {path}", {})
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, path):
        return self._do("GET", path)

    def post(self, path, body=None):
        return self._do("POST", path, body)

    def delete(self, path):
        return self._do("DELETE", path)


def answers(*typed):
    """An input() stand-in that plays the answers back in order, then reports the end of input."""
    queue = list(typed)

    def ask(_prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return ask


TOPICS = {
    "topics": [
        {"topic_id": "github-trending", "name": "GitHub Trending"},
        {"topic_id": "crypto", "name": "Crypto"},
    ]
}
ARMOR = {
    "armor": {
        "helmet": {"name": "Helm of Old Habits"},
        "chest": None,
        "gloves": None,
        "boots": None,
        "sword": None,
        "shield": None,
    }
}
MADE = {
    "created": {
        "topic_id": "global",
        "version": "v1",
        "placement": {"equipped": True, "slot": "helmet", "scope": "global", "displaced": None},
        "item": {"name": "Helm of Plain Speaking", "rarity": "epic", "durability": 24, "max_durability": 24},
    }
}


def guide(api, *typed):
    out = io.StringIO()
    return gc.guided_body(api, answers(*typed), out), out.getvalue()


# --- the guided conversation --------------------------------------------------------------------


def test_pressing_enter_all_the_way_makes_armor_the_bear_names_and_places():
    body, _ = guide(
        FakeApi({"GET /equipment": ARMOR}), "Lead with the most useful fact.", "", "", "", "", "", ""
    )

    assert body == {"text": "Lead with the most useful fact.", "equip": True}


def test_a_ring_asks_which_topic_and_ties_it_to_that_topic():
    api = FakeApi({"GET /topics": TOPICS})

    body, out = guide(api, "Name the repository.", "2", "2", "", "", "", "")  # ring, then the second topic

    assert body["topic_id"] == "crypto" and "slot" not in body
    assert "Crypto (crypto)" in out and "GitHub Trending (github-trending)" in out


def test_armor_shows_what_each_slot_holds_and_what_choosing_it_would_replace():
    api = FakeApi({"GET /equipment": ARMOR})

    body, out = guide(api, "Be brief.", "1", "2", "", "", "", "")  # the first named slot is the helmet

    assert body["slot"] == "helmet"
    assert "Helmet: worn: Helm of Old Habits  <- it would be replaced" in out
    assert "Chest: empty" in out


def test_the_rarity_the_name_and_the_backpack_can_all_be_chosen():
    body, out = guide(
        FakeApi({"GET /equipment": ARMOR}), "Be brief.", "", "", "5", "Plain Speaking", "n", "y"
    )

    assert body == {"text": "Be brief.", "rarity": "epic", "theme": "Plain Speaking", "equip": False}
    assert "Rarity:   epic" in out and "no, into the backpack" in out


def test_the_summary_says_what_will_be_made_before_anything_is():
    _, out = guide(FakeApi({"GET /equipment": ARMOR}), "Be brief.", "", "", "", "", "", "")

    for line in ("Guidance: Be brief.", "armor for every topic", "suggested by the bear", "Rarity:   rolled"):
        assert line in out


@pytest.mark.parametrize(
    "typed",
    [
        ("q",),
        ("Be brief.", "q"),
        ("Be brief.", "", "q"),
        ("Be brief.", "", "", "q"),
        ("Be brief.", "", "", "", "", "", "", "n"),  # ...answered the loot-drop question, then declined
    ],
)
def test_backing_out_at_any_point_makes_nothing(typed):
    api = FakeApi({"GET /equipment": ARMOR})

    assert guide(api, *typed)[0] is None
    assert not [c for c in api.calls if c[0] == "POST"]


def test_a_ring_with_no_topics_says_so_and_stops():
    body, out = guide(FakeApi({"GET /topics": {"topics": []}}), "Be brief.", "2")

    assert body is None and "no topics yet" in out


def test_text_that_is_too_long_is_asked_for_again():
    body, out = guide(FakeApi({"GET /equipment": ARMOR}), "x" * 1001, "Short one.", "", "", "", "", "", "")

    assert body["text"] == "Short one." and "the limit is 1000" in out


def test_a_number_that_is_not_an_option_is_asked_for_again():
    _, out = guide(FakeApi({"GET /equipment": ARMOR}), "Be brief.", "9", "", "", "", "", "", "")

    assert "Please type a number from 1 to 2." in out


def test_running_out_of_input_never_hangs():
    no_text, _ = guide(FakeApi())
    assert no_text is None
    body, _ = guide(FakeApi({"GET /equipment": ARMOR}), "Be brief.")  # then every question takes its default
    assert body == {"text": "Be brief.", "equip": True}


# --- creating -------------------------------------------------------------------------------------


def test_creating_posts_the_body_and_says_what_was_made_and_where():
    api = FakeApi({"POST /equipment": MADE})
    out = io.StringIO()

    code = gc.create(api, {"text": "Be brief.", "slot": "helmet"}, answers(), out)

    assert code == 0
    assert api.calls == [("POST", "/equipment", {"text": "Be brief.", "slot": "helmet"})]
    assert out.getvalue().strip() == (
        "Made Helm of Plain Speaking (epic, durability 24/24), "
        "and BloggerBear is now wearing it in the helmet slot."
    )


def test_creating_with_no_body_runs_the_conversation_first():
    api = FakeApi({"GET /equipment": ARMOR, "POST /equipment": MADE})

    code = gc.create(api, None, answers("Be brief.", "", "", "", "", "", "", ""), io.StringIO())

    assert code == 0 and api.calls[-1] == ("POST", "/equipment", {"text": "Be brief.", "equip": True})


def test_cancelling_the_conversation_makes_nothing_and_exits_nonzero():
    api = FakeApi({"GET /equipment": ARMOR})
    out = io.StringIO()

    assert gc.create(api, None, answers("q"), out) == 1
    assert "Cancelled" in out.getvalue() and not [c for c in api.calls if c[0] == "POST"]


def test_a_refusal_from_the_api_is_shown_plainly():
    api = FakeApi({"POST /equipment": ApiError(409, "all 5 rings are worn: choose one to replace")})
    out = io.StringIO()

    assert gc.create(api, {"text": "x", "topic_id": "t"}, answers(), out) == 1
    assert "Could not make it: all 5 rings are worn" in out.getvalue()


def test_the_message_covers_the_backpack_a_ring_and_a_replacement():
    backpack = {"created": {**MADE["created"], "placement": {"equipped": False}}}
    ring = {
        "created": {**MADE["created"], "placement": {"equipped": True, "slot": "ring", "displaced": None}}
    }
    swap = {
        "created": {
            **MADE["created"],
            "placement": {
                "equipped": True,
                "slot": "helmet",
                "displaced": {"topic_id": "global", "version": "old"},
            },
        }
    }

    assert gc.describe_created(backpack).endswith("and put it in the backpack.")
    assert "as a ring for its topic" in gc.describe_created(ring)
    assert "in the helmet slot, replacing global old." in gc.describe_created(swap)


# --- deleting -------------------------------------------------------------------------------------

ROW = {
    "version": "v1",
    "name": "Helm of Plain Speaking",
    "rarity": "epic",
    "equipped": True,
    "prompt_changes": "Be brief.",
}
LIST = {"GET /prompt-refinements?topic_id=global": {"refinements": [ROW]}}


def test_deleting_shows_what_it_is_asks_and_deletes_on_yes():
    api = FakeApi({**LIST, "DELETE /prompt-refinements/global/v1": {"deleted": {}}})
    out = io.StringIO()

    code = gc.delete(api, "global", "v1", answers("y"), out=out)

    assert code == 0 and ("DELETE", "/prompt-refinements/global/v1", None) in api.calls
    text = out.getvalue()
    assert "Helm of Plain Speaking (epic), worn right now." in text and "keep their own record" in text
    assert "Deleted Helm of Plain Speaking." in text


@pytest.mark.parametrize("reply", ["", "n", "no", "maybe"])
def test_anything_but_yes_keeps_it(reply):
    api = FakeApi(LIST)
    out = io.StringIO()

    assert gc.delete(api, "global", "v1", answers(reply), out=out) == 1
    assert "Kept" in out.getvalue() and not [c for c in api.calls if c[0] == "DELETE"]


def test_yes_skips_the_question():
    api = FakeApi({**LIST, "DELETE /prompt-refinements/global/v1": {}})

    def never(_prompt):
        raise AssertionError("must not ask")

    assert gc.delete(api, "global", "v1", never, assume_yes=True, out=io.StringIO()) == 0


def test_gear_that_is_not_there_is_reported_not_deleted():
    api = FakeApi({"GET /prompt-refinements?topic_id=global": {"refinements": []}})
    out = io.StringIO()

    assert gc.delete(api, "global", "nope", answers("y"), out=out) == 1
    assert "There is no gear global / nope" in out.getvalue()


def test_a_failure_while_looking_up_or_deleting_is_shown():
    lookup = FakeApi({"GET /prompt-refinements?topic_id=global": ApiError(0, "network problem")})
    delete = FakeApi({**LIST, "DELETE /prompt-refinements/global/v1": ApiError(500, "boom")})
    out = io.StringIO()

    assert gc.delete(lookup, "global", "v1", answers("y"), out=out) == 1
    assert gc.delete(delete, "global", "v1", answers("y"), out=out) == 1
    assert (
        "Could not look it up: network problem" in out.getvalue()
        and "Could not delete it: boom" in out.getvalue()
    )


# --- wired into admin_cli -------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload


COMMON = ["--api-url", "https://api.example.com", "--region", "ap-southeast-2"]


def run_cli(argv, responses):
    """Run admin_cli with signed requests answered from `responses` ("METHOD /path" -> payload)."""
    sent = []

    def fake(method, api_url, path, region, body=None):
        sent.append((method, path, body))
        return FakeResponse(200, responses.get(f"{method} {path}", {}))

    with patch("admin_cli.signed_request", side_effect=fake):
        admin_cli.main(COMMON + argv)
    return sent


def test_create_with_flags_sends_exactly_those_fields(capsys):
    sent = run_cli(
        [
            "equipment",
            "create",
            "--text",
            "Be brief.",
            "--scope",
            "global",
            "--slot",
            "helmet",
            "--rarity",
            "epic",
            "--theme",
            "Plain Speaking",
        ],
        {"POST /equipment": MADE},
    )

    assert sent == [
        (
            "POST",
            "/equipment",
            {
                "text": "Be brief.",
                "equip": True,
                "scope": "global",
                "slot": "helmet",
                "rarity": "epic",
                "theme": "Plain Speaking",
            },
        )
    ]
    assert "Made Helm of Plain Speaking" in capsys.readouterr().out


def test_create_for_a_topic_into_the_backpack_and_with_a_swap():
    sent = run_cli(
        [
            "equipment",
            "create",
            "--text",
            "x",
            "--topic-id",
            "crypto",
            "--no-equip",
            "--replace",
            "crypto",
            "v9",
        ],
        {"POST /equipment": MADE},
    )

    assert sent[0][2] == {
        "text": "x",
        "equip": False,
        "topic_id": "crypto",
        "replace": {"topic_id": "crypto", "version": "v9"},
    }


def test_create_with_no_text_starts_the_conversation(monkeypatch):
    typed = iter(["Be brief.", "", "", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(typed))

    sent = run_cli(["equipment", "create"], {"GET /equipment": ARMOR, "POST /equipment": MADE})

    assert sent[-1] == ("POST", "/equipment", {"text": "Be brief.", "equip": True})


def test_a_cancelled_or_refused_create_exits_nonzero(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "q")

    with pytest.raises(SystemExit) as caught:
        run_cli(["equipment", "create"], {})

    assert caught.value.code == 1


def test_delete_asks_unless_told_yes():
    sent = run_cli(
        ["equipment", "delete", "global", "v1", "--yes"],
        {
            **{k.split(" ", 1)[1] and k: v for k, v in LIST.items()},
            "DELETE /prompt-refinements/global/v1": {},
        },
    )

    assert ("DELETE", "/prompt-refinements/global/v1", None) in sent


def test_create_refuses_a_rarity_that_does_not_exist():
    with pytest.raises(SystemExit):
        run_cli(["equipment", "create", "--text", "x", "--rarity", "mythic"], {})


# --- loot drops -------------------------------------------------------------------------------------


def test_the_conversation_asks_about_the_loot_drop_and_can_decline_it():
    body, out = guide(FakeApi({"GET /equipment": ARMOR}), "Be brief.", "", "", "", "", "", "n", "y")

    assert body == {"text": "Be brief.", "equip": True, "announce": False}
    assert "Loot drop: no" in out


def test_by_default_the_drop_is_announced_and_the_summary_says_so():
    body, out = guide(FakeApi({"GET /equipment": ARMOR}), "Be brief.", "", "", "", "", "", "", "")

    assert "announce" not in body and "Loot drop: yes, posted to the Musings" in out


def test_gear_that_goes_to_the_backpack_is_not_asked_about_a_drop():
    body, out = guide(FakeApi({"GET /equipment": ARMOR}), "Be brief.", "", "", "", "", "n", "y")

    assert body["equip"] is False and "Post a loot drop" not in out and "Loot drop:" not in out


def test_the_message_says_when_a_loot_drop_was_posted():
    posted = {"created": {**MADE["created"], "loot_drop": "m-1"}}

    assert gc.describe_created(posted).endswith("A loot drop has been posted to the Musings.")
    assert "loot drop" not in gc.describe_created(MADE)


def test_create_can_stay_quiet_and_announce_can_be_run_later():
    quiet = run_cli(["equipment", "create", "--text", "x", "--no-announce"], {"POST /equipment": MADE})
    later = run_cli(["equipment", "announce", "global", "2026-09-22T00:00:00+00:00"], {})

    assert quiet[0][2]["announce"] is False
    assert later[0][:2] == ("POST", "/prompt-refinements/global/2026-09-22T00%3A00%3A00%2B00%3A00/announce")


def test_approve_and_equip_can_stay_quiet_too():
    approve = run_cli(["refinements", "approve", "t", "v1", "--no-announce"], {})
    equip = run_cli(["equipment", "equip", "t", "v1", "--no-announce"], {})
    loud = run_cli(["equipment", "equip", "t", "v1"], {})

    assert approve[0][2] == {"announce": False} and equip[0][2] == {"announce": False}
    assert loud[0][2] == {}  # announcing is the default
