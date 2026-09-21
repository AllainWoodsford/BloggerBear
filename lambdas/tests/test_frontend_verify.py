"""Tests for frontend/verify.js, run under Node (skipped when Node isn't installed).

The browser side of feedback verification: hold the one-use token, wait out its not-before time,
do the proof of work when asked, and recover quietly. A fake `fetch` plays the server; the same
proof of work is also checked against the Python server's own check, so the two cannot drift.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from common import feedback_verification as fv

NODE = shutil.which("node")
VERIFY_JS = Path(__file__).resolve().parents[2] / "frontend" / "verify.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Runs one scenario and prints a JSON summary. A fake server scripts the responses in order.
_RUNNER = """
const verify = require(process.argv[1]);
const { webcrypto } = require("crypto");
const scenario = JSON.parse(require("fs").readFileSync(0, "utf8"));

const log = [];          // what the page did, in order
let clock = 1000;        // fake time, advanced by sleep()
const script = (scenario.responses || []).slice();

function fakeResponse(spec) {
  return {
    ok: spec.status >= 200 && spec.status < 300,
    status: spec.status,
    json: () => Promise.resolve(spec.body),
    clone() { return fakeResponse(spec); },
  };
}

const fetchImpl = (url, init) => {
  const spec = script.shift();
  if (!spec) throw new Error("unexpected request: " + url);
  const entry = { url: url.replace("http://api", ""), method: (init && init.method) || "GET" };
  if (init && init.body) entry.body = JSON.parse(init.body);
  log.push(entry);
  return Promise.resolve(fakeResponse(spec));
};

const sleep = (ms) => { log.push({ sleep: ms }); clock += ms; return Promise.resolve(); };
const now = () => clock;

const config = {
  apiUrl: "http://api",
  articleId: scenario.articleId || "art-1",
  payload: scenario.payload || { vote: "up" },
  fetch: fetchImpl, sleep, now,
  subtle: webcrypto.subtle,
  onWorking: () => log.push({ working: true }),
};
if (scenario.hold) config.verification = verify.hold(scenario.hold, now);

const run = scenario.solve
  ? verify.solveWork(scenario.solve.token, scenario.solve.bits, { subtle: webcrypto.subtle, sleep })
      .then((nonce) => ({ nonce }))
  : verify.submit(config).then((outcome) => ({
      closed: outcome.closed || null,
      status: outcome.response ? outcome.response.status : null,
      log,
      left: script.length,
    }));
run.then((out) => process.stdout.write(JSON.stringify(out)));
"""


def run(scenario: dict) -> dict:
    result = subprocess.run(
        [NODE, "-e", _RUNNER, str(VERIFY_JS)],
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def open_status(token="tok-1", wait_ms=800, pow_bits=0):
    return {
        "status": 200,
        "body": {
            "open": True,
            "verification": {"token": token, "wait_ms": wait_ms, "pow_bits": pow_bits},
        },
    }


def refused(reason, retry_after_ms=None):
    return {
        "status": 403,
        "body": {
            "error": "verification failed",
            "verification": {"reason": reason, "retry_after_ms": retry_after_ms},
        },
    }


CREATED = {"status": 201, "body": {"status": "recorded"}}
POSTS = lambda out: [e for e in out["log"] if e.get("method") == "POST"]  # noqa: E731


# --- the proof of work agrees with the server's ---------------------------------------------------


@pytest.mark.parametrize("bits", [1, 4, 8, 10, 12])
def test_the_browsers_answer_is_accepted_by_the_servers_check(bits):
    token = "v1.some-token-value.sig"

    nonce = run({"solve": {"token": token, "bits": bits}})["nonce"]

    assert fv.work_is_valid(token, nonce, bits) is True
    # ...and it is the smallest one, like the server's own search would find.
    assert not any(fv.work_is_valid(token, n, bits) for n in range(nonce))


# --- the happy path ----------------------------------------------------------------------------------


def test_it_fetches_a_token_waits_out_its_delay_and_sends_it_with_the_payload():
    out = run(
        {
            "payload": {"vote": "down", "comment": "Please add a chart.", "extra_note": ""},
            "responses": [open_status("tok-1", wait_ms=800), CREATED],
        }
    )

    assert out["status"] == 201 and out["left"] == 0
    assert out["log"] == [
        {"url": "/articles/art-1/feedback-status", "method": "GET"},
        {"sleep": 800},
        {
            "url": "/articles/art-1/feedback",
            "method": "POST",
            "body": {
                "vote": "down",
                "comment": "Please add a chart.",
                "extra_note": "",
                "token": "tok-1",
            },
        },
    ]


def test_a_token_the_page_already_holds_is_used_without_asking_again():
    out = run(
        {
            "hold": {"token": "tok-held", "wait_ms": 0, "pow_bits": 0},
            "responses": [CREATED],
        }
    )

    assert out["status"] == 201
    assert [e.get("method") for e in out["log"] if "method" in e] == ["POST"]
    assert POSTS(out)[0]["body"]["token"] == "tok-held"


def test_a_person_who_read_the_article_waits_for_nothing():
    # The token was issued at the start of the page and its delay is long past.
    out = run({"hold": {"token": "tok", "wait_ms": 0, "pow_bits": 0}, "responses": [CREATED]})

    assert {"sleep": 0} in out["log"]
    assert all(e.get("sleep", 0) == 0 for e in out["log"])


def test_when_the_server_has_verification_off_it_sends_without_a_token():
    out = run(
        {
            "responses": [
                {"status": 200, "body": {"open": True}},
                CREATED,
            ]
        }
    )

    assert out["status"] == 201
    assert "token" not in POSTS(out)[0]["body"]


# --- proof of work -------------------------------------------------------------------------------


def test_when_work_is_asked_for_it_is_done_and_sent():
    out = run({"responses": [open_status("tok-w", wait_ms=0, pow_bits=8), CREATED]})

    assert out["status"] == 201
    assert {"working": True} in out["log"]
    post = POSTS(out)[0]["body"]
    assert fv.work_is_valid("tok-w", post["work"], 8)


def test_no_work_is_done_when_none_is_asked_for():
    out = run({"responses": [open_status("tok", pow_bits=0), CREATED]})

    assert {"working": True} not in out["log"]
    assert "work" not in POSTS(out)[0]["body"]


# --- recovering quietly ---------------------------------------------------------------------------


def test_too_early_waits_as_long_as_the_server_said_and_sends_the_same_token_again():
    out = run(
        {
            "responses": [
                open_status("tok-1", wait_ms=0),
                refused("too_early", retry_after_ms=300),
                CREATED,
            ]
        }
    )

    assert out["status"] == 201
    assert [e["body"]["token"] for e in POSTS(out)] == ["tok-1", "tok-1"]
    assert {"sleep": 350} in out["log"]  # the server's 300 plus a little


@pytest.mark.parametrize("reason", ["used", "expired", "invalid", "wrong_article", "missing", "work"])
def test_a_refused_token_is_replaced_with_a_fresh_one_and_the_send_is_retried(reason):
    out = run(
        {
            "responses": [
                open_status("tok-old", wait_ms=0),
                refused(reason),
                open_status("tok-new", wait_ms=0),
                CREATED,
            ]
        }
    )

    assert out["status"] == 201
    assert [e["body"]["token"] for e in POSTS(out)] == ["tok-old", "tok-new"]


def test_if_feedback_closed_in_the_meantime_it_says_so_instead_of_retrying():
    closed = {"open": False, "reason": "rate_limit", "label": "Rate limit", "retry_at": None}
    out = run(
        {
            "responses": [
                open_status("tok-old", wait_ms=0),
                refused("used"),
                {"status": 200, "body": closed},
            ]
        }
    )

    assert out["closed"] == closed and out["status"] is None
    assert len(POSTS(out)) == 1


def test_it_gives_up_rather_than_loop_if_the_server_keeps_refusing():
    out = run(
        {
            "responses": [
                open_status("t1", wait_ms=0),
                refused("used"),
                open_status("t2", wait_ms=0),
                refused("used"),
                open_status("t3", wait_ms=0),
                refused("used"),
            ]
        }
    )

    assert out["status"] == 403  # the final answer is handed back
    assert len(POSTS(out)) == 3 and out["left"] == 0


def test_a_refusal_it_cannot_fix_is_handed_back_as_it_is():
    out = run(
        {
            "hold": {"token": "tok", "wait_ms": 0, "pow_bits": 0},
            "responses": [
                {
                    "status": 403,
                    "body": {"error": "verification failed", "verification": {"reason": "surprise"}},
                }
            ],
        }
    )

    assert out["status"] == 403 and len(POSTS(out)) == 1


@pytest.mark.parametrize("status", [201, 400, 404, 422, 423, 429, 503])
def test_every_other_response_is_handed_straight_back(status):
    out = run(
        {
            "hold": {"token": "tok", "wait_ms": 0, "pow_bits": 0},
            "responses": [{"status": status, "body": {"feedback": {"open": False}}}],
        }
    )

    assert out["status"] == status and len(POSTS(out)) == 1


def test_the_payload_is_not_changed_by_sending():
    out = run(
        {
            "payload": {"vote": "up", "comment": None, "extra_note": ""},
            "responses": [open_status(wait_ms=0), refused("used"), open_status("t2", wait_ms=0), CREATED],
        }
    )

    assert [{k: v for k, v in e["body"].items() if k not in ("token",)} for e in POSTS(out)] == [
        {"vote": "up", "comment": None, "extra_note": ""}
    ] * 2
