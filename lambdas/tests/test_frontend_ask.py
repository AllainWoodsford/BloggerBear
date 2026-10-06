"""The operator's assistant page (frontend/ask.html, ask.js, ask.css).

Two kinds of test. The page's rules are checked as text: it is deployed, it never parses a string
as HTML or code, it stores nothing but the two PKCE values, it is not indexed or linked to, and it
does nothing where its settings are missing. The pure functions in ask.js (PKCE, the API's limits,
the history) are run under Node, skipped when Node isn't installed, as test_frontend_verify.py does.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import ops_agent_handler

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
INFRA = ROOT / "infra"
NEW_FILES = ("ask.html", "ask.js", "ask.css")
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def _code(js: str) -> str:
    """ask.js without its comments, so a rule about what the code does isn't tripped by the
    comment that states the rule."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}),])\s*//.*$", "", js)


def _between(text: str, start: str, end: str) -> str:
    """The part of `text` from `start` up to the next `end`."""
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


# --- deployed -----------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_page_and_its_files_are_in_each_environments_deploy_list(env):
    text = (INFRA / "environments" / env / "main.tf").read_text(encoding="utf-8")
    files = re.search(r"frontend_files = \{(.*?)\n  \}", text, re.S).group(1)

    for name, content_type in (
        ("ask.html", "text/html"),
        ("ask.css", "text/css"),
        ("ask.js", "application/javascript"),
    ):
        assert (FRONTEND / name).is_file()
        assert re.search(rf'"{re.escape(name)}"\s*=\s*"{content_type}"', files), (env, name)


def test_the_page_loads_its_own_files_and_the_generated_config_first():
    html = _read("ask.html")

    assert '<link rel="stylesheet" href="ask.css" />' in html
    assert html.index('<script src="config.js"></script>') < html.index('<script src="ask.js"></script>')
    # Nothing else: no app.js, and no script from anywhere but this site.
    assert re.findall(r"<script[^>]*>", html) == ['<script src="config.js">', '<script src="ask.js">']


# --- configuration ------------------------------------------------------------------------------


def test_dev_gives_the_page_its_settings_from_the_modules_outputs():
    text = (INFRA / "environments" / "dev" / "main.tf").read_text(encoding="utf-8")
    config = re.search(r'resource "aws_s3_object" "frontend_config" \{(.*?)\n\}', text, re.S).group(1)

    assert "window.OPS_ASSISTANT = {" in config
    assert 'askUrl: "${trimsuffix(module.ops_assistant.mcp_url, "/mcp")}/ask"' in config
    assert 'hostedUiDomain: "${module.ops_assistant.hosted_ui_domain}"' in config
    assert 'clientId: "${module.ops_assistant.app_client_id}"' in config
    assert 'scope: "${module.ops_assistant.read_scope}"' in config
    # The same string the module registers as the callback and logout URL.
    assert 'redirectUri: "${local.site_url}/ask.html",' in config
    # Shown on the page, so a command copied from a dev card is not run against production.
    assert 'environment: "dev"' in config
    assert 'callback_urls = ["${local.site_url}/ask.html"]' in text
    assert 'logout_urls   = ["${local.site_url}/ask.html"]' in text


@pytest.mark.parametrize("env", ["dev", "production"])
def test_each_environment_gives_the_page_its_own_settings(env):
    """Production has the assistant too (docs/enhancements/alexa-plus.md, section 4.4): the same
    settings from its own module, and the environment named so a copied command says which admin
    API it is for."""
    text = (INFRA / "environments" / env / "main.tf").read_text(encoding="utf-8")
    config = re.search(r'resource "aws_s3_object" "frontend_config" \{(.*?)\n\}', text, re.S).group(1)

    assert 'askUrl: "${trimsuffix(module.ops_assistant.mcp_url, "/mcp")}/ask"' in config
    assert 'hostedUiDomain: "${module.ops_assistant.hosted_ui_domain}"' in config
    assert 'clientId: "${module.ops_assistant.app_client_id}"' in config
    assert f'environment: "{env}"' in config


def test_productions_assistant_requires_mfa_and_is_the_only_one_with_account_wide_data():
    production = (INFRA / "environments" / "production" / "main.tf").read_text(encoding="utf-8")
    dev = (INFRA / "environments" / "dev" / "main.tf").read_text(encoding="utf-8")
    module = re.search(r'^module "ops_assistant" \{\n(.*?)^\}', production, re.S | re.M).group(1)

    assert re.search(r'^\s*mfa_configuration\s*=\s*"ON"$', module, re.M)
    assert re.search(r"^\s*account_wide_data\s*=\s*true$", module, re.M)
    assert re.search(r'^\s*environment_name\s*=\s*"production"$', module, re.M)
    dev_module = re.search(r'^module "ops_assistant" \{\n(.*?)^\}', dev, re.S | re.M).group(1)
    assert "account_wide_data" not in dev_module and "waf_log_groups" not in dev_module


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_sites_headers_are_loosened_only_for_what_the_page_needs(env):
    module = (INFRA / "modules" / "static-site" / "main.tf").read_text(encoding="utf-8")
    variables = (INFRA / "modules" / "static-site" / "variables.tf").read_text(encoding="utf-8")
    dev = (INFRA / "environments" / env / "main.tf").read_text(encoding="utf-8")

    # Off unless asked for, and never wider than this site's own pages.
    assert re.search(r'variable "allow_microphone" \{\s*type\s*=\s*bool\s*default\s*=\s*false', variables)
    assert 'microphone=(${var.allow_microphone ? "self" : ""})' in module
    assert "allow_microphone = true" in dev
    # The one extra host a script may call: Cognito's hosted domain, for the token exchange.
    site = re.search(r'module "static_site" \{(.*?)\n\}', dev, re.S).group(1)
    hosts = re.search(r"extra_connect_src = \[(.*?)\]", site, re.S).group(1).split(",")
    assert [host.strip() for host in hosts if host.strip()] == [
        "module.public_api_cdn.domain_name",
        "module.ops_assistant.hosted_ui_domain",
    ]
    # Still no inline script or style, and forms still post only to the site itself.
    assert "\"script-src 'self'\"" in module and "\"form-action 'self'\"" in module


def test_the_page_says_it_is_unavailable_until_the_script_finds_its_settings():
    html = _read("ask.html")
    code = _code(_read("ask.js"))

    # The only section not hidden in the HTML is the one that says so.
    assert re.search(r'<section id="ask-unavailable">\s*<p>The assistant is not available here\.</p>', html)
    assert '<section id="ask-gate" hidden>' in html
    assert '<section id="ask-app" hidden>' in html
    # The script stops before it wires or shows anything when the settings are missing.
    guard = code.index("var config = readConfig(root.OPS_ASSISTANT);")
    assert re.search(r"if \(!config \|\|[^)]*\) \{\s*return;\s*\}", code[guard:])
    for later in ("addEventListener", "showGate(", ".hidden = ", "fetch("):
        assert code.index(later, guard) > code.index("return;", guard), later


# --- nothing from the API is ever HTML or code --------------------------------------------------


@pytest.mark.parametrize("name", NEW_FILES)
def test_no_html_or_code_sinks_in_the_new_files(name):
    text = _read(name)

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "new Function"):
        assert sink not in text, sink
    assert not re.search(r"\beval\s*\(", text)
    assert not re.search(r"set(Timeout|Interval)\(\s*[\"'`]", text)
    # No inline handlers (onclick="..."), in the HTML or set as attributes from the script.
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", text, re.I)
    assert not re.search(r"setAttribute\(\s*[\"']on", text)
    assert "javascript:" not in text.lower()


def test_the_page_has_no_inline_script_or_style():
    html = _read("ask.html")

    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html)
    assert "<style" not in html and not re.search(r"<[^>]*\sstyle\s*=", html)


def test_untrusted_text_is_marked_and_a_command_is_never_run_or_fetched():
    code = _code(_read("ask.js"))

    assert 'key === "untrusted"' in code
    assert '"ask-untrusted-mark", "Unverified"' in code
    assert ".ask-untrusted-mark" in _read("ask.css")
    # The only three requests: the token exchange, the wake call and the question, the last two
    # to the same address.
    assert len(re.findall(r"\.fetch\(", code)) == 3
    assert '"/oauth2/token"' in code and code.count(".fetch(config.askUrl") == 2


# --- storage ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEW_FILES)
def test_no_local_storage_or_cookies(name):
    text = _code(_read(name)) if name.endswith(".js") else _read(name)

    assert "localStorage" not in text
    assert "document.cookie" not in text and "indexedDB" not in text


def test_session_storage_holds_only_the_two_pkce_values_and_they_are_removed_on_return():
    code = _code(_read("ask.js"))

    uses = re.findall(r"sessionStorage\.(\w+)\(([^,)]*)", code)
    assert uses, "the PKCE values must survive the trip to the sign-in page"
    # The two PKCE values, and when the assistant was last woken (a time and nothing else).
    assert {key for _, key in uses} == {"VERIFIER_KEY", "STATE_KEY", "WARMED_KEY"}
    assert {method for method, _ in uses} == {"setItem", "getItem", "removeItem"}
    assert "sessionStorage.setItem(WARMED_KEY, String(Date.now()))" in code
    assert "sessionStorage[" not in code
    # Removed as soon as they are read, before the state is checked or the code is exchanged.
    start = code.index("function start()")
    removed = code.index("sessionStorage.removeItem(STATE_KEY)", start)
    assert removed < code.index("returnedState !== expectedState", start) < code.index("/oauth2/token", start)
    # The token is a variable: nothing named like one is ever handed to storage.
    assert not re.search(r"setItem\([^)]*(token|Token)", code)


def test_the_assistant_is_woken_once_at_sign_in_and_nothing_about_it_is_shown():
    """The owner's ask: start the Lambdas as the operator signs in, in the background, and at
    most once in five minutes."""
    code = _code(_read("ask.js"))
    start = code.index("function start()")

    # Right after the page is shown with a fresh token, and nowhere else.
    signed_in = code.index("accessToken = tokens.access_token", start)
    assert code.index("showApp();\n        warmUp();", start) > signed_in
    assert code.count("warmUp()") == 2  # the definition and that one call
    wake = _between(code, "function warmUp()", "\n  }\n")
    assert "body: JSON.stringify({ warm: true })" in wake
    assert 'Authorization: "Bearer " + accessToken' in wake
    assert "shouldWarm(last, Date.now())" in wake and "!accessToken" in wake
    # The time is written before the request goes, so two quick loads send one.
    assert wake.index("sessionStorage.setItem(WARMED_KEY") < wake.index(".fetch(config.askUrl")
    # Nothing reaches the screen or the speaker, whatever comes back.
    for shown in ("textContent", "status", "speak", "render", "showGate", "response"):
        assert shown not in wake, shown
    assert ".then(noop, noop)" in wake
    assert "var WARM_COOLDOWN_MS = 5 * 60 * 1000;" in code


@needs_node
def test_a_wake_call_is_held_back_for_five_minutes_after_the_last_one():
    script = """
    const ask = require(process.argv[1]);
    const now = 1_000_000_000;
    const gap = ask.WARM_COOLDOWN_MS;
    process.stdout.write(JSON.stringify({
      cooldown: gap,
      never: ask.shouldWarm(0, now),
      missing: ask.shouldWarm(null, now),
      garbage: ask.shouldWarm("not a time", now),
      justNow: ask.shouldWarm(now - 1000, now),
      almost: ask.shouldWarm(now - gap + 1, now),
      due: ask.shouldWarm(now - gap, now),
      future: ask.shouldWarm(now + 60000, now),
    }));
    """
    done = subprocess.run(
        [NODE, "-e", script, str(FRONTEND / "ask.js")], capture_output=True, text=True, check=True
    )
    result = json.loads(done.stdout)

    assert result["cooldown"] == 300_000
    assert result["never"] and result["missing"] and result["garbage"] and result["due"]
    assert not result["justNow"] and not result["almost"]
    assert result["future"]  # a clock that was changed must not stop it for ever


def test_the_code_and_state_leave_the_address_bar_before_the_exchange():
    code = _code(_read("ask.js"))
    start = code.index("function start()")

    replaced = code.index('history.replaceState(null, "", root.location.pathname)', start)
    assert replaced < code.index(".fetch(", start)


def test_sign_in_is_pkce_s256_with_a_checked_state():
    code = _code(_read("ask.js"))

    assert 'code_challenge_method: "S256"' in code and 'response_type: "code"' in code
    assert 'subtle.digest("SHA-256"' in code and "getRandomValues" in code
    assert "Math.random" not in code
    assert '"/logout?"' in code  # sign-out goes through the hosted logout endpoint


# --- not for visitors ---------------------------------------------------------------------------


def test_the_page_asks_not_to_be_indexed_and_robots_txt_does_not_advertise_it():
    assert '<meta name="robots" content="noindex" />' in _read("ask.html")
    # A Disallow line would publish the path, and stop a crawler from ever seeing the noindex.
    assert "ask" not in _read("robots.txt").lower().replace("asked", "")


def test_nothing_else_links_to_the_page():
    sources = [path for path in FRONTEND.rglob("*") if path.is_file() and path.name not in NEW_FILES]
    sources.append(ROOT / "lambdas" / "common" / "static_pages.py")

    for path in sources:
        if path.suffix not in (".html", ".js", ".py", ".txt", ".css"):
            continue
        assert "ask.html" not in path.read_text(encoding="utf-8"), path.name


def test_the_page_is_named_for_what_it_is_and_never_as_an_alexa_product():
    # What a visitor is shown or told: the page without its comments (which name the design doc's
    # file), and the script's and stylesheet's code.
    page = re.sub(r"<!--.*?-->", "", _read("ask.html"), flags=re.S)
    for text in (page, _code(_read("ask.js")), _code(_read("ask.css"))):
        assert "alexa" not in text.lower()
    assert "<h1>BloggerBear operator assistant</h1>" in _read("ask.html")


# --- accessibility ------------------------------------------------------------------------------


def test_the_answer_and_the_status_are_announced_and_every_button_has_a_label():
    html = _read("ask.html")

    assert re.search(r'id="ask-status"[^>]*aria-live="polite"', html)
    assert re.search(r'id="ask-conversation"[^>]*aria-live="polite"', html)
    assert re.search(r'id="ask-gate-message"[^>]*aria-live="polite"', html)
    assert '<label for="ask-question">' in html
    for button in re.findall(r"<button[^>]*>(.*?)</button>", html, re.S):
        assert button.strip(), "a button with no text"
    assert all('type="' in tag for tag in re.findall(r"<button[^>]*>", html))


def test_push_to_talk_works_from_the_keyboard_and_without_holding():
    code = _code(_read("ask.js"))
    css = _read("ask.css")

    for event in ("pointerdown", "pointerup", "keydown", "keyup", "click"):
        assert f'talkButton.addEventListener("{event}"' in code, event
    assert 'event.key === " "' in code and 'event.key === "Enter"' in code
    assert "held >= HOLD_MS" in code  # a short press toggles; only a real hold stops on release
    assert "root.SpeechRecognition || root.webkitSpeechRecognition" in code
    # The browser taking the pointer (a scroll, a long press on a phone) is not the operator
    # letting go: it must not stop the microphone.
    assert 'talkButton.addEventListener("pointercancel", pressCancelled)' in code
    assert re.search(
        r"function pressCancelled\(\) \{\s*pressedAt = 0;\s*lastPressAt = Date.now\(\);\s*\}", code
    )
    assert 'talkButton.addEventListener("contextmenu"' in code
    assert "touch-action: none;" in css and "-webkit-touch-callout: none;" in css
    assert ".ask-talk:focus-visible" in css and ".ask-button:focus-visible" in css
    # The only animation is switched on for people who have not asked for less motion.
    assert css.count("animation:") == 1
    block = css[css.index("@media (prefers-reduced-motion: no-preference)") :]
    assert block.index("animation:") < block.index("@keyframes")


def test_speech_stops_for_a_new_question_and_can_be_muted():
    code = _code(_read("ask.js"))
    ask = code[code.index("function ask(text)") :]

    assert ask.index("stopSpeaking();") < ask.index(".fetch(")
    assert "speak(spokenText(result.answer, result.findings))" in code
    assert re.search(r"function speak\(text\) \{\s*if \(muted \|\|", code)



def test_speech_is_spoken_in_pieces_on_the_next_tick_and_held_until_it_ends():
    code = _code(_read("ask.js"))
    say = code[code.index("function say(text, onDone)") : code.index("function speak(text)")]

    assert "speechChunks(text, SPEECH_CHUNK_CHARS)" in say
    # Never in the same tick as cancel(), and every utterance held until it ends.
    assert "root.setTimeout(function" in say and "SPEECH_DELAY_MS" in say
    assert "speechQueue.push(utterance)" in say
    # cancel() only when something is speaking or queued.
    assert re.search(r"if \(synth && \(synth\.speaking \|\| synth\.pending\)\) \{\s*synth\.cancel\(\);", code)
    # The language is the browser's English tag, never <html lang>'s bare "en".
    assert "documentElement.lang" not in code


def test_speech_is_unlocked_by_a_click_and_a_refused_answer_waits_for_the_next_tap():
    """iOS speaks only from a user activation, and a touch's pointerdown is not one: the unlock
    listens for click. It counts as done only once the silent utterance starts, and an answer the
    browser refused (\"not-allowed\") is kept and spoken from the next tap."""
    code = _code(_read("ask.js"))

    assert 'doc.addEventListener("click", unlockSpeech, true)' in code
    assert 'addEventListener("pointerdown", unlockSpeech' not in code
    unlock = code[code.index("function unlockSpeech(event)") :]
    unlock = unlock[: unlock.index("\n  }\n")]
    assert "silent.onstart = function () {" in unlock and "speechUnlocked = true;" in unlock
    assert "blockedSpeech" in unlock
    say = code[code.index("function say(text, onDone)") : code.index("function speak(text)")]
    assert 'event.error === "not-allowed"' in say and "blockedSpeech = text;" in say


def test_the_voice_test_listens_only_after_its_sample_has_been_spoken():
    code = _code(_read("ask.js"))
    test = code[code.index("function testVoice()") :]

    assert "whenSampleDone(function () {" in test
    assert "}, 2500);" not in test
    say = code[code.index("function say(text, onDone)") : code.index("function speak(text)")]
    assert "position === chunks.length - 1" in say and "SPEECH_DONE_FALLBACK_MS" in say


def test_recognition_shows_words_as_heard_and_asks_once_it_ends():
    code = _code(_read("ask.js"))
    listener = code[code.index("function createListener(env)") : code.index("function pickVoice(")]
    done = _between(code, "function listeningDone(result, options)", "var listener = createListener(")

    assert "current.interimResults = true;" in listener
    assert "current.lang = env.lang;" in listener and "lang: LANG," in code
    # One short session at a time: a tap must still stop by itself when the person stops speaking.
    assert "current.continuous = false;" in listener and "continuous = true" not in code
    # The listener never asks: it hands the words over once, and the page asks with them.
    assert "ask(" not in listener
    assert listener.count("env.onDone(") == 1
    assert done.count("ask(result.heard);") == 1 and "options.onHeard(result.heard);" in done
    assert "onDone: listeningDone," in code
    # Listening cancels speech, and does not start while a question is being answered.
    start = _between(code, "function startListening(onHeard, onFailed, press)", "function stopListening()")
    assert "if (listening || busy || !Recognition || voiceUnusable) {" in start
    assert start.index("stopSpeaking();") < start.index("listener.start(")


def test_a_held_button_keeps_listening_and_only_a_press_of_the_talk_button_counts_as_held():
    code = _code(_read("ask.js"))
    html = _read("ask.html")

    assert "var LISTEN_MAX_MS = 60000;" in code
    held = code[code.index("isHeld: function (options) {") :]
    held = held[: held.index("},")]
    assert "options.press === true" in held and "pressedAt !== 0" in held and "pressStarted" in held
    assert "Date.now() - pressedAt >= HOLD_MS" in held
    # Only the talk button's own press says so; the voice test and a screen reader's click do not.
    assert code.count("startListening(null, null, true);") == 1
    press_down = code[code.index("function pressDown()") : code.index("function pressUp()")]
    assert "startListening(null, null, true);" in press_down
    # A held key repeats: only the first keydown is the press.
    assert "if (!event.repeat) {" in code
    # The ceiling ends the press, so the click its release makes is not taken for a tap.
    ceiling = _between(code, "onCeiling: function () {", "onDone: listeningDone,")
    assert re.search(r"if \(pressedAt\) \{\s*pressedAt = 0;\s*skipClick = true;", ceiling)
    assert "if (skipClick || Date.now() - lastPressAt < CLICK_AFTER_PRESS_MS) {" in code
    assert "for up to a minute" in _between(html, 'id="ask-talk-hint"', 'id="ask-speech-broken"')


def test_a_browser_whose_recognition_cannot_work_gets_the_text_controls_and_loses_the_button():
    code = _code(_read("ask.js"))
    html = _read("ask.html")

    # Decided from the errors received, never from the browser's name.
    assert "userAgent" not in code and "navigator.vendor" not in code
    assert re.search(r'<p id="ask-speech-broken" class="ask-hint" hidden>', html)
    retire = code[code.index("function retireVoice()") : code.index("function listeningDone(")]
    for line in (
        "voiceUnusable = true;",
        "talkButton.hidden = true;",
        'el("ask-talk-hint").hidden = true;',
        'el("ask-speech-broken").hidden = false;',
        "showTextControls(true);",
        "questionInput.focus();",
    ):
        assert line in retire, line
    done = _between(code, "function listeningDone(result, options)", "var listener = createListener(")
    # Any failure that tells the operator to type opens the box to type in.
    assert re.search(r'if \(verdict !== "none"\) \{\s*showTextControls\(true\);', done)
    assert 'if (verdict === "unusable" || serviceFailures >= SERVICE_FAILURES_MAX) {' in done
    assert "var SERVICE_FAILURES_MAX = 2;" in code
    # Words heard clear the count: one bad moment on a working browser does not cost the button.
    assert re.search(r"if \(result\.heard\) \{\s*serviceFailures = 0;", done)
    # Said through the status line, which is announced.
    assert "status.textContent = message;" in done
    # The voice test reports the same finding, in the same words.
    test = code[code.index("function testVoice()") :]
    assert "if (voiceUnusable) {" in test and "lastVoiceFailure" in test
    assert '"Listening: did not work this time. " + message' in test
    # Signing out, or typing a question, ends listening without asking what was heard.
    gate = code[code.index("function showGate(message)") : code.index("function showApp()")]
    assert "abandonListening();" in gate and "stopListening();" not in gate


# --- ways to start, and the text-based controls --------------------------------------------------

STARTER_QUESTIONS = [
    "What needs my attention?",
    "What are your prior suggestions?",
    "I'm new, where should I start?",
    "What can you do?",
    "How does the project work?",
]


def test_the_ways_to_start_are_plain_text_under_the_title_and_match_the_quick_buttons():
    html = _read("ask.html")
    app = html[html.index('<section id="ask-app" hidden>') :]

    # Plain text: a list, first thing in the signed-in page, with no control inside it.
    starters = app[app.index('<div class="ask-starters">') : app.index('<div class="ask-toolbar">')]
    assert re.findall(r"<li>(.*?)</li>", starters) == STARTER_QUESTIONS
    assert "<button" not in starters and "<a " not in starters
    # The same questions, in the same order, are the quick buttons.
    quick = app[app.index('<div class="ask-quick"') : app.index('<form id="ask-form"')]
    assert re.findall(r"<button[^>]*ask-quick-question[^>]*>(.*?)</button>", quick) == STARTER_QUESTIONS
    assert quick.count("<button") == len(STARTER_QUESTIONS)
    # The toolbar is as it was.
    toolbar = app[app.index('<div class="ask-toolbar">') : app.index('<ul id="ask-voice-report"')]
    labels = re.findall(r">([^<>]+)</button>", toolbar)
    assert labels == ["New briefing", "Mute voice", "Test voice", "Sign out"]


def test_the_text_based_controls_are_closed_until_asked_for():
    html = _read("ask.html")
    code = _code(_read("ask.js"))

    toggle = re.search(r'<button type="button" id="ask-text-toggle"[^>]*>\s*(.*?)\s*</button>', html, re.S)
    assert toggle.group(1) == "Show text-based controls"
    assert 'aria-expanded="false"' in toggle.group(0)
    assert 'aria-controls="ask-text-controls"' in toggle.group(0)
    # The quick questions and the typed one are both inside the block the toggle opens...
    block = html[html.index('<div id="ask-text-controls" class="ask-text-controls" hidden>') :]
    block = block[: block.index('<p id="ask-status"')]
    assert 'id="ask-briefing"' in block and '<form id="ask-form"' in block
    assert block.count("ask-quick-question") == len(STARTER_QUESTIONS)
    # ...and push to talk is not.
    assert html.index('id="ask-talk"') < html.index('id="ask-text-toggle"')
    assert 'id="ask-talk"' not in block

    show = code[code.index("function showTextControls(show)") : code.index("function wireTalkButton()")]
    assert "textControls.hidden = !show;" in show
    assert 'textToggle.setAttribute("aria-expanded", show ? "true" : "false");' in show
    assert '"Hide text-based controls" : "Show text-based controls"' in show
    assert "showTextControls(textControls.hidden);" in code
    # A browser that cannot listen has nothing but typing, so the controls open themselves.
    no_speech = code[code.index("if (!Recognition) {", code.index("function wireTalkButton()")) :]
    assert "showTextControls(true);" in no_speech[: no_speech.index("return;")]
    # Nothing about the choice is stored.
    assert "localStorage" not in code


def test_a_quick_question_asks_its_own_label_as_a_new_conversation():
    code = _code(_read("ask.js"))

    quick = code[code.index("function askQuick(event)") : code.index("var quickButtons")]
    assert "if (busy) {" in quick
    assert "event.currentTarget.textContent" in quick
    assert quick.index("turns = [];") < quick.index("clear(conversation);") < quick.index("ask(label")
    assert 'doc.querySelectorAll(".ask-quick-question")' in code
    assert 'addEventListener("click", askQuick)' in code
    # Still only the three requests the page ever makes (the wake call is the third).
    assert len(re.findall(r"\.fetch\(", code)) == 3


def test_the_stylesheets_carry_the_prefixes_current_browsers_still_need():
    """Run through Autoprefixer for browsers still in use, the only declaration it adds to these
    files is the WebKit prefix on user-select. There is no CSS build step here beyond stripping
    comments and whitespace (scripts/minify_frontend.py), so the prefix is written by hand."""
    for name in ("ask.css", "styles.css"):
        css = re.sub(r"/\*.*?\*/", "", _read(name), flags=re.S)
        for rule in re.findall(r"\{([^{}]*)\}", css):
            if re.search(r"(?<![-\w])user-select\s*:", rule):
                assert "-webkit-user-select" in rule, (name, rule.strip())


def test_the_voice_test_is_on_the_page_and_reports_as_text():
    html = _read("ask.html")
    code = _code(_read("ask.js"))

    assert re.search(r'<button type="button" id="ask-voice-test"[^>]*>Test voice</button>', html)
    assert re.search(r'<ul id="ask-voice-report"[^>]*aria-live="polite"[^>]*hidden>', html)
    assert 'el("ask-voice-test").addEventListener("click", testVoice)' in code
    report = code[code.index("function report(lines)") : code.index("function testVoice()")]
    assert 'make("li", "", line)' in report


# --- the pure functions, under Node -------------------------------------------------------------

_RUNNER = """
const ask = require(process.argv[1]);
const { webcrypto } = require("crypto");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));

(async () => {
  const config = ask.readConfig(input.config);
  const out = {
    limits: [ask.QUESTION_MAX_CHARS, ask.HISTORY_MAX_TURNS, ask.TURN_MAX_CHARS],
    challenge: await ask.pkceChallenge(input.verifier, webcrypto.subtle),
    random: [ask.randomString(32, webcrypto), ask.randomString(32, webcrypto)],
    configs: input.badConfigs.map((raw) => ask.readConfig(raw)),
    config,
    authorizeUrl: ask.authorizeUrl(config, "st&ate", "chal"),
    tokenBody: ask.tokenRequestBody(config, "the code", "ver"),
    logoutUrl: ask.logoutUrl(config),
    questions: input.questions.map((text) => ask.checkQuestion(text)),
    history: ask.buildHistory(input.turns),
    rows: ask.flattenRows(input.where, "", false),
    kinds: input.findings.map((finding) => ask.cardKind(finding)),
    spoken: ask.spokenText(input.answer, input.findings),
    langs: input.langs.map((tag) => ask.speechLang(tag)),
    chunks: ask.speechChunks(input.longAnswer, 180),
    shortChunks: ask.speechChunks("One. Two!  Three?", 180),
    numberChunks: ask.speechChunks(input.numberAnswer, 180),
    noChunks: ask.speechChunks("   ", 180),
    messages: input.errorCodes.map((code) => ask.recognitionMessage(code)),
    verdicts: input.verdictCases.map(([code, online]) => ask.recognitionVerdict(code, online)),
    joined: input.heardSets.map((pieces) => ask.joinHeard(pieces)),
    keepOn: input.keepCases.map((state) => ask.keepListening(state)),
    listenMax: ask.LISTEN_MAX_MS,
    voices: input.voiceSets.map((set) => {
      const picked = ask.pickVoice(set.voices, set.lang);
      return picked ? picked.name : null;
    }),
    environments: input.envConfigs.map((raw) => ask.readConfig(raw).environment),
  };
  process.stdout.write(JSON.stringify(out));
})().catch((err) => { console.error(err); process.exit(1); });
"""

_CONFIG = {
    "askUrl": "https://abc123.execute-api.ap-southeast-2.amazonaws.com/dev/ask",
    "hostedUiDomain": "bloggerbear-dev-ops.auth.ap-southeast-2.amazoncognito.com",
    "clientId": "client-1",
    "scope": "bloggerbear-ops/read",
    "redirectUri": "https://d111.cloudfront.net/ask.html",
}
_HELD = {"held": True, "stopping": False, "code": "", "elapsedMs": 5000, "quickEnds": 0}
_COMMAND = 'python scripts/admin_cli.py articles rewrite 01J8 -i "the draft was cut short"'
_FINDINGS = [
    {"kind": "truncated", "suggestion": {"action": "Rewrite it", "command": _COMMAND, "what_it_does": "x"}},
    {"kind": "alarm", "suggestion": {"action": "Look at the alarm", "command": None, "what_it_does": None}},
    {"kind": "odd", "suggestion": None},
]


@pytest.fixture(scope="module")
def node_result():
    turns = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"turn {i}"} for i in range(9)]
    turns.append({"role": "assistant", "text": "  " + "a" * 1500})
    turns.append({"role": "assistant", "text": "   "})  # the API refuses an empty turn
    turns.append({"role": "system", "text": "not a role the API takes"})
    payload = {
        # RFC 7636, appendix B.
        "verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "config": _CONFIG,
        "badConfigs": [
            None,
            {},
            {**_CONFIG, "clientId": ""},
            {**_CONFIG, "askUrl": "http://insecure.example/ask"},
            {**_CONFIG, "hostedUiDomain": "evil.example/path?x="},
            {k: v for k, v in _CONFIG.items() if k != "scope"},
        ],
        "questions": ["  What needs my attention?  ", "", "   ", "q" * 500, "q" * 501, None],
        "turns": turns,
        "where": {
            "article_id": "01J8",
            "count": 3,
            "missing": None,
            "untrusted": {"path": "/<script>alert(1)</script>", "nested": {"matched": "x"}},
        },
        "findings": _FINDINGS,
        "answer": f"One draft was cut short. Run {_COMMAND} to fix it.",
        "langs": ["en-AU", "en-us", "en", "fr-FR", None, "en-AU; DROP", "  en-GB  "],
        "longAnswer": "Since yesterday crypto did not publish. "
        + "Its draft was cut short, so it is held in the inbox and the authoring function was "
        + "throttled around two in the morning while the retry ran out of attempts and gave up. "
        + "Spend is normal. "
        + ("word " * 60),
        "numberAnswer": "AI spend was US$12.40 this week, up from 9.80. Python 3.11 is fine.",
        "errorCodes": [
            "not-allowed",
            "network",
            "audio-capture",
            "no-speech",
            "aborted",
            "made-up",
            "constructor",
            "service-not-allowed",
            "silent",
            "no-start",
            "start-failed",
            "offline",
        ],
        "verdictCases": [
            ["", True],
            ["no-speech", True],
            ["aborted", True],
            ["not-allowed", True],
            ["audio-capture", True],
            ["service-not-allowed", True],
            ["language-not-supported", True],
            ["network", True],
            ["network", False],
            ["silent", True],
            ["no-start", True],
            ["start-failed", True],
            ["made-up", True],
        ],
        "heardSets": [
            ["what needs", " my   attention ", "", None, "today"],
            ["what needs", "What needs my attention"],
            ["what needs my attention", "what needs my attention"],
            ["what needs", "what needsmore"],
            ["no", "no way"],
            [],
            None,
            ["  ", ""],
        ],
        "keepCases": [
            _HELD,
            {**_HELD, "code": "no-speech"},
            {**_HELD, "held": False},
            {**_HELD, "stopping": True},
            {**_HELD, "code": "network"},
            {**_HELD, "code": "silent"},
            {**_HELD, "code": "aborted"},
            {**_HELD, "elapsedMs": 60000},
            {**_HELD, "elapsedMs": 59999},
            {**_HELD, "quickEnds": 3},
            None,
            {**_HELD, "heard": True, "code": "aborted"},
            {**_HELD, "heard": True, "code": "network"},
            {**_HELD, "heard": True, "code": "silent"},
            {**_HELD, "heard": True, "code": "not-allowed"},
            {**_HELD, "heard": True, "code": "aborted", "held": False},
            {**_HELD, "heard": True, "code": "aborted", "quickEnds": 3},
        ],
        "voiceSets": [
            {"lang": "en-AU", "voices": [{"name": "us", "lang": "en-US"}, {"name": "au", "lang": "en_AU"}]},
            {
                "lang": "en-NZ",
                "voices": [
                    {"name": "fr", "lang": "fr-FR"},
                    {"name": "def", "lang": "en-GB", "default": True},
                ],
            },
            {"lang": "en-NZ", "voices": [{"name": "fr", "lang": "fr-FR"}, {"name": "gb", "lang": "en-GB"}]},
            {"lang": "en-AU", "voices": [{"name": "fr", "lang": "fr-FR"}]},
            {"lang": "en-AU", "voices": None},
        ],
        "envConfigs": [
            {**_CONFIG, "environment": "dev"},
            {**_CONFIG, "environment": "Production; rm"},
            _CONFIG,
            {**_CONFIG, "environment": 7},
        ],
    }
    done = subprocess.run(
        [NODE, "-e", _RUNNER, str(FRONTEND / "ask.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_the_pkce_challenge_matches_the_rfc_7636_example(node_result):
    assert node_result["challenge"] == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@needs_node
def test_the_verifier_is_long_random_and_url_safe(node_result):
    first, second = node_result["random"]

    assert first != second
    # 32 random bytes: 43 characters, inside RFC 7636's 43 to 128, from its unreserved set.
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)


@needs_node
def test_the_pages_limits_are_the_handlers(node_result):
    assert node_result["limits"] == [
        ops_agent_handler.QUESTION_MAX_CHARS,
        ops_agent_handler.HISTORY_MAX_TURNS,
        ops_agent_handler.TURN_MAX_CHARS,
    ]


@needs_node
def test_a_question_is_trimmed_and_refused_when_empty_or_too_long(node_result):
    trimmed, empty, blank, longest, too_long, missing = node_result["questions"]

    assert trimmed == {"question": "What needs my attention?"}
    assert longest == {"question": "q" * 500}
    for refused in (empty, blank, too_long, missing):
        assert "question" not in refused and refused["error"]


@needs_node
def test_history_is_the_last_six_turns_each_within_the_limit(node_result):
    history = node_result["history"]

    assert len(history) == 6
    assert [turn["text"] for turn in history[:5]] == [f"turn {i}" for i in range(4, 9)]
    assert history[-1] == {"role": "assistant", "text": "a" * 1000}
    assert all(set(turn) == {"role", "text"} and turn["role"] in ("user", "assistant") for turn in history)
    # What the page would send passes the handler's own check.
    ops_agent_handler._validated({"question": "q", "history": history})


@needs_node
def test_settings_are_all_or_nothing(node_result):
    assert node_result["config"] == {**_CONFIG, "environment": ""}
    assert node_result["configs"] == [None] * 6


@needs_node
def test_the_sign_in_urls_are_built_from_the_settings_and_escaped(node_result):
    host = "https://bloggerbear-dev-ops.auth.ap-southeast-2.amazoncognito.com"
    redirect = "https%3A%2F%2Fd111.cloudfront.net%2Fask.html"

    assert node_result["authorizeUrl"] == (
        f"{host}/oauth2/authorize?response_type=code&client_id=client-1&redirect_uri={redirect}"
        "&scope=bloggerbear-ops%2Fread&state=st%26ate&code_challenge=chal&code_challenge_method=S256"
    )
    assert node_result["tokenBody"] == (
        f"grant_type=authorization_code&client_id=client-1&code=the%20code&redirect_uri={redirect}"
        "&code_verifier=ver"
    )
    assert node_result["logoutUrl"] == f"{host}/logout?client_id=client-1&logout_uri={redirect}"


@needs_node
def test_everything_under_untrusted_is_marked_however_deep(node_result):
    assert node_result["rows"] == [
        {"label": "article_id", "text": "01J8", "untrusted": False},
        {"label": "count", "text": "3", "untrusted": False},
        {"label": "path", "text": "/<script>alert(1)</script>", "untrusted": True},
        {"label": "nested matched", "text": "x", "untrusted": True},
    ]


@needs_node
def test_each_finding_gets_the_right_card_and_a_command_is_never_spoken(node_result):
    assert node_result["kinds"] == ["fix", "look", "noticed"]
    assert node_result["spoken"] == "One draft was cut short. Run the command on screen to fix it."


# --- how-to cards, help blocks and tables --------------------------------------------------------
# The page's own renderers (renderCard, renderTable), run under Node against a stand-in document
# that records what was built. The stand-in has createElement, createTextNode and textContent and
# nothing else: assigning innerHTML to one of its nodes throws.

_RENDER_RUNNER = """
const ask = require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));

function node(tag) {
  const made = {
    tag, className: "", textContent: "", children: [], attrs: {}, listeners: {},
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };
  for (const sink of ["innerHTML", "outerHTML"]) {
    Object.defineProperty(made, sink, { set() { throw new Error(sink + " was assigned"); } });
  }
  return made;
}
const doc = {
  createElement: node,
  createTextNode: (text) => ({ tag: "#text", textContent: String(text), children: [], attrs: {} }),
};
function plain(made) {
  if (!made) { return null; }
  return {
    tag: made.tag, cls: made.className || "", text: made.textContent || "", attrs: made.attrs || {},
    type: made.type || "", children: (made.children || []).map(plain),
  };
}
function find(made, test, found) {
  found = found || [];
  if (test(made)) { found.push(made); }
  (made.children || []).forEach((child) => find(child, test, found));
  return found;
}

const cards = input.findings.map((finding) => {
  const copied = [];
  const onCopy = (code, button) => copied.push([code.textContent, button.textContent]);
  const card = ask.renderCard(doc, finding, onCopy);
  find(card, (made) => made.tag === "button").forEach((button) => button.listeners.click());
  return { card: plain(card), copied };
});
process.stdout.write(JSON.stringify({
  cards,
  headings: input.findings.map((finding) => ask.cardHeading(finding)),
  kinds: input.findings.map((finding) => ask.cardKind(finding)),
  tables: input.tables.map((table) => plain(ask.renderTable(doc, table))),
  models: input.tables.map((table) => ask.tableModel(table)),
  spoken: ask.spokenText(input.answer, input.findings),
}));
"""

_HELP_TEXT = (
    "usage: admin_cli.py topics update [-h] [--name NAME] topic_id\n\noptions:\n  --name NAME  <b>bold</b>\n"
)
_HELP_COMMAND = "python scripts/admin_cli.py topics update --help"
_TEMPLATE = "python scripts/admin_cli.py topics delete <topic_id>"
_HOW_TO = [
    {
        "kind": "how_to",
        "id": "help-topics-update",
        "noticed": "topics update: Update a topic",
        "where": {"command": "topics update"},
        "suggestion": {"action": "Read the options", "command": _HELP_COMMAND, "what_it_does": "Prints it."},
        "help": _HELP_TEXT,
    },
    {
        "kind": "how_to",
        "id": "topics-delete-template",
        "noticed": "topics delete: Delete a topic",
        "where": {"command": "topics delete"},
        "suggestion": {"action": "Fill in the template", "command": _TEMPLATE, "what_it_does": "Deletes."},
        "destructive": True,
        "warning": "The assistant never fills one in.",
    },
    _FINDINGS[0],
]
_TABLES = [
    {
        "title": "Topics (2 of 7)",
        "columns": ["Name", "Topic id", "Runs"],
        "rows": [["Crypto", "crypto", 3], ["<img src=x onerror=alert(1)>", "hn"], "not a row"],
    },
    {"title": "", "columns": [], "rows": []},
    None,
    {"columns": ["a"], "rows": [[{"an": "object"}]]},
]


def _find(tree: dict, **wanted) -> list[dict]:
    found = [tree] if all(tree.get(key) == value for key, value in wanted.items()) else []
    for child in tree["children"]:
        found.extend(_find(child, **wanted))
    return found


def _texts(tree: dict) -> str:
    return tree["text"] + "".join(_texts(child) for child in tree["children"])


@pytest.fixture(scope="module")
def rendered():
    payload = {
        "findings": _HOW_TO,
        "tables": _TABLES,
        "answer": f"Run {_HELP_COMMAND} to see them, or {_TEMPLATE} to delete.",
    }
    done = subprocess.run(
        [NODE, "-e", _RENDER_RUNNER, str(FRONTEND / "ask.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a_how_to_card_reads_as_guidance_and_shows_the_help_as_text_in_a_block(rendered):
    help_card, _, ordinary = (entry["card"] for entry in rendered["cards"])

    assert rendered["headings"] == ["How to", "How to", "Noticed"]
    labels = [dt["text"] for dt in _find(help_card, tag="dt")]
    assert labels[0] == "How to" and "Noticed" not in labels and "Next" in labels
    assert "ask-card-how-to" in help_card["cls"]
    # The help reaches the page whole, in its own block, as text: markup in it stays characters.
    (block,) = _find(help_card, tag="pre", cls="ask-help")
    assert block["text"] == _HELP_TEXT and "<b>bold</b>" in block["text"]
    assert block["attrs"]["tabindex"] == "0" and block["attrs"]["role"] == "region"
    assert block["attrs"]["aria-label"] == "Help text for topics update: Update a topic"
    # The Copy button copies the one line that prints the help, never the help.
    assert rendered["cards"][0]["copied"] == [[_HELP_COMMAND, "Copy"]]
    (button,) = _find(help_card, tag="button")
    assert button["type"] == "button" and button["attrs"]["aria-label"] == "Copy the command"
    # An ordinary finding is as it was.
    assert [dt["text"] for dt in _find(ordinary, tag="dt")][:2] == ["Noticed", "Suggested"]
    assert _find(ordinary, tag="pre", cls="ask-help") == [] and "how-to" not in ordinary["cls"]


@needs_node
def test_a_destructive_card_warns_in_words_and_copies_the_template_with_its_placeholders(rendered):
    card, copied = rendered["cards"][1]["card"], rendered["cards"][1]["copied"]

    assert "ask-card-destructive" in card["cls"]
    (warning,) = _find(card, tag="p", cls="ask-warning")
    assert warning["attrs"]["role"] == "note"
    assert _texts(warning) == "Template, not filled in. The assistant never fills one in."
    # Shown as it is, and copied as it is: the placeholder is still a placeholder.
    (code,) = _find(card, tag="code")
    assert code["text"] == _TEMPLATE
    assert copied == [[_TEMPLATE, "Copy template"]] and "<topic_id>" in copied[0][0]
    (button,) = _find(card, tag="button")
    assert button["attrs"]["aria-label"] == "Copy the template, with its placeholders"
    # Neither a how-to command nor a template is ever read aloud.
    assert rendered["kinds"] == ["fix", "fix", "fix"]
    assert rendered["spoken"] == "Run the command on screen to see them, or the command on screen to delete."


@needs_node
def test_a_table_is_a_real_table_with_a_caption_and_text_cells(rendered):
    table, empty, missing, odd = rendered["tables"]

    assert empty is None and missing is None
    assert table["tag"] == "div" and table["cls"] == "ask-table-wrap"
    assert table["attrs"] == {"tabindex": "0", "role": "region", "aria-label": "Topics (2 of 7)"}
    (real,) = _find(table, tag="table")
    assert [child["tag"] for child in real["children"]] == ["caption", "thead", "tbody"]
    assert real["children"][0]["text"] == "Topics (2 of 7)"
    heads = _find(real["children"][1], tag="th")
    assert [th["text"] for th in heads] == ["Name", "Topic id", "Runs"]
    assert all(th["attrs"] == {"scope": "col"} for th in heads)
    rows = _find(real["children"][2], tag="tr")
    assert len(rows) == 2  # the thing that was not a row is left out
    assert [cell["text"] for cell in rows[0]["children"]] == ["Crypto", "crypto", "3"]
    assert rows[0]["children"][0]["tag"] == "th" and rows[0]["children"][0]["attrs"] == {"scope": "row"}
    # Markup in a cell is characters, and a short row is padded to the columns.
    assert [cell["text"] for cell in rows[1]["children"]] == ["<img src=x onerror=alert(1)>", "hn", ""]
    assert rendered["models"][3] == {"title": "Table", "columns": ["a"], "rows": [[""]]}
    assert _find(odd, tag="caption")[0]["text"] == "Table"


def test_the_page_takes_tables_and_how_to_cards_from_the_answer_and_builds_them_as_text():
    code = _code(_read("ask.js"))
    css = _read("ask.css")

    assert "tables: Array.isArray(body.tables) ? body.tables : []" in code
    assert "renderTable(doc, table)" in code and "renderCard(doc, finding, copyCommand)" in code
    assert 'finding.kind === "how_to"' in code and "finding.destructive === true" in code
    # What is copied is the text of the node that shows the command.
    assert "clipboard.writeText(code.textContent)" in code
    # Still one way to make a node, and it sets text, never markup.
    assert code.count("createElement(") == 1 and "node.textContent = String(text)" in code
    # The help and the table scroll inside their own box on a narrow screen.
    for selector in (".ask-help", ".ask-table-wrap", ".ask-table caption", ".ask-warning"):
        assert selector in css, selector
    help_block = css[css.index(".ask-help {") : css.index("}", css.index(".ask-help {"))]
    assert "overflow: auto" in help_block and "white-space: pre" in help_block and "monospace" in help_block
    wrap = css[css.index(".ask-table-wrap {") : css.index("}", css.index(".ask-table-wrap {"))]
    assert "overflow-x: auto" in wrap
    assert ".ask-help:focus-visible" in css and ".ask-table-wrap:focus-visible" in css


def test_the_pages_table_limits_are_the_agents():
    code = _code(_read("ask.js"))
    from ops_agent import policy

    assert f"var TABLE_MAX_ROWS = {policy.TABLE_MAX_ROWS};" in code
    assert f"var TABLE_MAX_COLUMNS = {policy.TABLE_MAX_COLUMNS};" in code


def test_the_minified_copy_is_built_from_these_files_and_still_has_the_new_renderers(tmp_path):
    """frontend-dist is generated at deploy time (scripts/minify_frontend.py, which mirrors every
    file in frontend/): the new code needs no list to be on, and survives minifying."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("minify_for_ask", ROOT / "scripts" / "minify_frontend.py")
    minify = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(minify)
    except ImportError:
        pytest.skip("the minifiers are not installed")

    minify.minify_frontend(FRONTEND, tmp_path)

    built = (tmp_path / "ask.js").read_text(encoding="utf-8")
    for kept in ("how_to", "ask-help", "ask-table-wrap", "Copy template", "renderTable", "createListener"):
        assert kept in built, kept
    assert "innerHTML" not in built
    assert ".ask-table-wrap" in (tmp_path / "ask.css").read_text(encoding="utf-8")


@needs_node
def test_speech_uses_the_browsers_english_tag_or_en_us(node_result):
    assert node_result["langs"] == ["en-AU", "en-us", "en-US", "en-US", "en-US", "en-US", "en-GB"]


@needs_node
def test_an_answer_is_spoken_in_whole_sentences_each_short_enough(node_result):
    chunks = node_result["chunks"]
    assert len(chunks) > 2
    assert all(0 < len(chunk) <= 180 for chunk in chunks)
    # Nothing lost, reordered or split inside a word, only runs of spaces made one. (Comparing
    # `.split()` of both sides missed a space put into "12.40": "12." and "40" are words too.)
    assert " ".join(chunks) == " ".join(
        (
            "Since yesterday crypto did not publish. Its draft was cut short, so it is held in the inbox and "
            "the authoring function was throttled around two in the morning while the retry ran out of "
            "attempts and gave up. Spend is normal. " + "word " * 60
        ).split()
    )
    assert chunks[0].startswith("Since yesterday crypto did not publish.")
    assert node_result["shortChunks"] == ["One. Two! Three?"]
    # A point inside a number ends no sentence: "US$12. 40" would be spoken "twelve dollars. forty".
    assert node_result["numberChunks"] == [
        "AI spend was US$12.40 this week, up from 9.80. Python 3.11 is fine."
    ]
    assert node_result["noChunks"] == []


@needs_node
def test_each_recognition_error_says_what_to_do(node_result):
    not_allowed, network, capture, no_speech, aborted, unknown, inherited = node_result["messages"][:7]
    assert "site settings" in not_allowed
    assert "Chrome or Edge" in network
    assert "microphone" in capture.lower()
    assert no_speech.startswith("Heard nothing")
    assert aborted == ""
    # An error the page does not know, even one named like an object's own property, is generic.
    assert unknown == inherited == "Speech recognition failed. Type your question instead."
    # A browser that has recognition and no service behind it: each way that shows up names the
    # browsers that can listen, and typing.
    service, silent, no_start, start_failed, offline = node_result["messages"][7:]
    assert "Opera" in network and "Opera" in silent
    for told in (service, silent, no_start, start_failed):
        assert "Chrome or Edge" in told and "type your question" in told
    assert "allow it and press again" in no_start
    assert "offline" in offline and "Chrome or Edge" not in offline


@needs_node
def test_what_a_recognition_failure_means_for_the_talk_button(node_result):
    assert node_result["verdicts"] == [
        "none",  # ended cleanly
        "none",  # nothing was said
        "none",  # the page stopped it
        "blocked",  # the microphone was refused: the operator can allow it
        "blocked",  # no microphone
        "unusable",  # the browser says it will not do it
        "unusable",
        "service",  # no speech service could be reached, though the browser is online
        "offline",  # ...or it is not online, which is not the browser's fault
        "service",  # ended without ever opening the microphone
        "service",  # never reported anything
        "service",  # start() threw
        "service",  # an error the page does not know
    ]


@needs_node
def test_words_heard_in_several_sessions_are_one_question_in_order_without_duplicates(node_result):
    assert node_result["joined"] == [
        "what needs my attention today",
        "What needs my attention",  # a session that gives back what is kept, and more, replaces it
        "what needs my attention",  # ...or gives back exactly what is kept
        "what needs what needsmore",  # only whole words count as the same beginning
        "no way",
        "",
        "",
        "",
    ]


@needs_node
def test_another_session_starts_only_while_held_unstopped_unfailed_and_under_the_ceiling(node_result):
    assert node_result["listenMax"] == 60000
    assert node_result["keepOn"] == [
        True, True, False, False, False, False, False, False, True, False, False,
        # Once words have been heard, a session the browser cut short is listened through too,
        # but never a refusal, a release or a run of sessions that go nowhere.
        True, True, True, False, False, False,
    ]  # fmt: skip


# --- listening, start to finish, with a scripted recogniser --------------------------------------
# createListener is given a recogniser the test scripts (to end early, raise errors, deliver words
# in pieces, or say nothing at all) and a clock the test moves, so what a held button and a broken
# browser do is run, not read.

_LISTEN_RUNNER = """
const ask = require(process.argv[1]);

function harness(Recognition) {
  const made = [];
  class Fake {
    constructor() { this.calls = []; made.push(this); }
    start() { this.calls.push("start"); if (Fake.startThrows) { throw new Error("no"); } }
    stop() { this.calls.push("stop"); }
    abort() { this.calls.push("abort"); }
    fire(type, event) { if (this["on" + type]) { this["on" + type](event || {}); } }
    words(...pieces) {
      this.fire("result", { results: pieces.map(([transcript, isFinal]) =>
        Object.assign([{ transcript }], { isFinal })) });
    }
  }
  let now = 1000;
  let timers = [];
  let nextId = 1;
  const log = { states: [], texts: [], done: [], ceilings: 0 };
  const world = { held: false };
  const listener = ask.createListener({
    Recognition: Recognition === null ? undefined : Fake,
    lang: "en-AU",
    now: () => now,
    setTimeout: (fn, ms) => { timers.push({ id: nextId, at: now + ms, fn }); return nextId++; },
    clearTimeout: (id) => { timers = timers.filter((timer) => timer.id !== id); },
    isHeld: (options) => options.press === true && world.held,
    onState: (value) => log.states.push(value),
    onText: (text) => log.texts.push(text),
    onCeiling: () => { log.ceilings += 1; },
    onDone: (result, options) => log.done.push(Object.assign({ press: options.press === true }, result)),
  });
  function advance(ms) {
    const until = now + ms;
    for (;;) {
      const due = timers.filter((timer) => timer.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!due) { break; }
      timers = timers.filter((timer) => timer !== due);
      now = due.at;
      due.fn();
    }
    now = until;
  }
  return { Fake, made, log, world, listener, advance, last: () => made[made.length - 1] };
}

const out = {};

{ // A tap: one session, which the browser ends when the person stops speaking.
  const h = harness();
  const began = h.listener.start({});
  const again = h.listener.start({});
  const r = h.last();
  out.settings = [r.lang, r.interimResults, r.continuous, r.maxAlternatives];
  r.fire("start"); r.fire("audiostart");
  r.words(["what needs", false]);
  r.words(["what needs my attention", true]);
  r.fire("end");
  h.advance(120000);
  out.tap = { began, again, sessions: h.made.length, log: h.log };
}

{ // A hold: the browser ends a session at each pause, and once for a long silence; the words of
  // all of them are one question, asked once, when the button is let go.
  const h = harness();
  h.world.held = true;
  h.listener.start({ press: true });
  let r = h.last();
  r.fire("audiostart"); r.words(["what needs", true]); r.fire("end");
  h.advance(3000);
  r = h.last();
  r.fire("audiostart"); r.words([" my", true], [" atten", false]);
  r.words([" my", true], [" attention", true]); r.fire("end");
  h.advance(8000);
  r = h.last();
  r.fire("audiostart"); r.fire("error", { error: "no-speech" }); r.fire("end");
  r = h.last();
  r.fire("audiostart"); r.words(["What needs my attention today", false]);
  const before = h.log.done.length;
  h.world.held = false;
  h.listener.stop();
  h.listener.stop();
  r.words(["What needs my attention today", true]);
  r.fire("end");
  h.advance(120000);
  out.hold = { before, sessions: h.made.length, calls: h.made.map((made) => made.calls), log: h.log };
}

{ // A hold the browser cuts short mid-question ("aborted", then a session that ends with no
  // sign of sound): the button is still down, so it keeps listening and asks once on release.
  const h = harness();
  h.world.held = true;
  h.listener.start({ press: true });
  let r = h.last();
  r.fire("audiostart"); r.words(["how is the finance", true]);
  r.fire("error", { error: "aborted" }); r.fire("end");
  h.advance(2000);
  r = h.last();
  r.fire("start"); r.fire("end");
  h.advance(2000);
  r = h.last();
  r.fire("audiostart"); r.words(["topic doing", true]);
  const before = h.log.done.length;
  h.world.held = false;
  h.listener.stop();
  r.fire("end");
  h.advance(120000);
  out.cutShort = { before, sessions: h.made.length, log: h.log };
}

{ // Recognition is there and nothing is behind it: an error, then the end. Held or not, the page
  // does not try again by itself.
  const h = harness();
  h.world.held = true;
  h.listener.start({ press: true });
  h.last().fire("start"); h.last().fire("error", { error: "network" }); h.last().fire("end");
  h.advance(120000);
  out.network = { sessions: h.made.length, log: h.log };
}

{ // ...or it ends at once, with no error and the microphone never opened.
  const h = harness();
  h.world.held = true;
  h.listener.start({ press: true });
  h.last().fire("start"); h.last().fire("end");
  h.advance(120000);
  out.silent = { sessions: h.made.length, log: h.log };
}

{ // ...or it reports nothing at all, ever.
  const h = harness();
  h.listener.start({});
  h.advance(9999);
  const early = h.log.done.length;
  h.advance(1);
  h.last().fire("error", { error: "aborted" }); h.last().fire("end");
  h.advance(120000);
  out.nothing = { early, calls: h.last().calls, log: h.log };
}

{ // ...or it reports an error and never the end.
  const h = harness();
  h.listener.start({});
  h.last().fire("error", { error: "service-not-allowed" });
  const early = h.log.done.length;
  h.advance(1000);
  out.noEnd = { early, log: h.log };
}

{ // ...or start() throws.
  const h = harness();
  h.Fake.startThrows = true;
  const began = h.listener.start({});
  out.throws = { began, log: h.log };
}

{ // No recognition at all: nothing starts.
  const h = harness(null);
  out.none = { began: h.listener.start({}), log: h.log };
}

{ // Nothing said, with the microphone open: not a broken browser.
  const h = harness();
  h.listener.start({});
  h.last().fire("audiostart"); h.last().fire("end");
  out.quiet = h.log;
}

{ // Held for longer than the ceiling: the question is ended for the operator, once.
  const h = harness();
  h.world.held = true;
  h.listener.start({ press: true });
  for (let i = 0; i < 11; i++) {
    const r = h.last();
    r.fire("audiostart"); h.advance(5000); r.words(["word " + i, true]); r.fire("end");
  }
  const r = h.last();
  r.fire("audiostart"); r.words(["and more", false]);
  h.advance(4999);
  const before = [h.log.ceilings, r.calls.slice()];
  h.advance(1);
  const after = [h.log.ceilings, r.calls.slice()];
  r.fire("end");
  h.advance(120000);
  out.ceiling = { before, after, sessions: h.made.length, log: h.log };
}

{ // Held, and sessions that end as fast as they start with nothing heard: it gives up, not loops.
  const h = harness();
  h.world.held = true;
  h.listener.start({ press: true });
  for (let i = 0; i < 10 && !h.log.done.length; i++) { h.last().fire("audiostart"); h.last().fire("end"); }
  out.loop = { sessions: h.made.length, log: h.log };
}

{ // Let go, and the browser never says it has ended: the words so far are still asked.
  const h = harness();
  h.listener.start({});
  h.last().fire("audiostart"); h.last().words(["publish it", false]);
  h.listener.stop();
  h.advance(3999);
  const early = h.log.done.length;
  h.advance(1);
  h.last().words(["publish it now", true]); h.last().fire("end");
  out.noEndAfterStop = { early, log: h.log };
}

{ // Abandoned (sign-out, or a typed question): nothing is asked, then or later.
  const h = harness();
  h.listener.start({});
  h.last().fire("audiostart"); h.last().words(["delete everything", true]);
  h.listener.abandon();
  h.last().fire("end");
  h.advance(120000);
  const restarted = h.listener.start({});
  out.abandoned = { calls: h.made[0].calls, restarted, log: h.log };
}

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def listened():
    done = subprocess.run(
        [NODE, "-e", _LISTEN_RUNNER, str(FRONTEND / "ask.js")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a_tap_listens_once_and_asks_once_when_the_browser_stops(listened):
    tap = listened["tap"]

    assert listened["settings"] == ["en-AU", True, False, 1]
    assert tap["began"] is True and tap["again"] is False  # a second start while listening is refused
    assert tap["sessions"] == 1  # not held: the session the browser ended is not followed by another
    assert tap["log"]["states"] == [True, False]
    assert tap["log"]["texts"] == ["what needs", "what needs my attention"]
    assert tap["log"]["done"] == [{"press": False, "heard": "what needs my attention", "code": ""}]


@needs_node
def test_a_held_button_listens_across_pauses_and_sends_one_question_on_release(listened):
    hold = listened["hold"]

    # Four sessions: one for each stretch of speech, and one that heard only silence.
    assert hold["sessions"] == 4
    assert hold["before"] == 0  # nothing is asked while the button is down
    # The button reads "listening" the whole time: no flicker between sessions.
    assert hold["log"]["states"] == [True, False]
    # The words on screen only ever grow, in order.
    assert hold["log"]["texts"] == [
        "what needs",
        "what needs my atten",
        "what needs my attention",
        "What needs my attention today",
        "What needs my attention today",
    ]
    # The last session gave back the whole question: it is kept once, not twice.
    assert hold["log"]["done"] == [{"press": True, "heard": "What needs my attention today", "code": ""}]
    # Only the last session is stopped, and only once however often release is reported.
    assert hold["calls"] == [["start"], ["start"], ["start"], ["start", "stop"]]


@needs_node
def test_a_hold_the_browser_cuts_short_keeps_listening_until_the_button_is_let_go(listened):
    cut = listened["cutShort"]

    assert cut["before"] == 0 and cut["sessions"] == 3
    assert cut["log"]["states"] == [True, False]
    assert cut["log"]["done"] == [{"press": True, "heard": "how is the finance topic doing", "code": ""}]


def test_the_talk_button_keeps_the_pointer_so_drifting_off_it_does_not_let_go():
    code = _code(_read("ask.js"))

    on = 'talkButton.addEventListener("'
    down = _between(code, on + 'pointerdown"', on + 'pointerup"')
    assert "talkButton.setPointerCapture(event.pointerId);" in down
    assert down.index("setPointerCapture") < down.index("pressDown();")
    leave = _between(code, on + 'pointerleave"', on + 'contextmenu"')
    assert 'event.pointerType === "mouse" && !holdsPointer(event.pointerId)' in leave
    assert "talkButton.hasPointerCapture(pointerId)" in code


@needs_node
def test_recognition_that_exists_but_cannot_work_is_reported_and_never_retried_by_itself(listened):
    assert listened["network"]["sessions"] == 1
    assert listened["network"]["log"]["done"] == [{"press": True, "heard": "", "code": "network"}]
    assert listened["silent"]["sessions"] == 1
    assert listened["silent"]["log"]["done"] == [{"press": True, "heard": "", "code": "silent"}]
    # Nothing for ten seconds: stopped by the page, and what the browser says after that is ignored.
    nothing = listened["nothing"]
    assert nothing["early"] == 0 and nothing["calls"] == ["start", "abort"]
    assert nothing["log"]["done"] == [{"press": False, "heard": "", "code": "no-start"}]
    assert nothing["log"]["states"] == [True, False]
    # An error with no "end" after it does not leave the page listening for ever.
    assert listened["noEnd"]["early"] == 0
    assert listened["noEnd"]["log"]["done"] == [{"press": False, "heard": "", "code": "service-not-allowed"}]
    assert listened["throws"]["began"] is False
    assert listened["throws"]["log"]["done"] == [{"press": False, "heard": "", "code": "start-failed"}]
    assert listened["throws"]["log"]["states"] == [True, False]
    assert listened["none"] == {"began": False, "log": {"states": [], "texts": [], "done": [], "ceilings": 0}}
    # Silence with the microphone open is the operator saying nothing, not the browser failing.
    assert listened["quiet"]["done"] == [{"press": False, "heard": "", "code": ""}]


@needs_node
def test_a_held_button_stops_at_the_ceiling_and_does_not_loop_on_sessions_that_go_nowhere(listened):
    ceiling = listened["ceiling"]

    assert ceiling["before"] == [0, ["start"]]
    assert ceiling["after"] == [1, ["start", "stop"]]  # at 60 seconds, to the millisecond
    assert ceiling["sessions"] == 12
    (done,) = ceiling["log"]["done"]
    assert done["heard"] == " ".join(f"word {i}" for i in range(11)) + " and more"
    assert ceiling["log"]["states"] == [True, False]
    loop = listened["loop"]
    assert loop["sessions"] == 3 and loop["log"]["done"] == [{"press": True, "heard": "", "code": ""}]


@needs_node
def test_letting_go_always_ends_in_one_question_and_abandoning_in_none(listened):
    stuck = listened["noEndAfterStop"]
    assert stuck["early"] == 0
    # Words the browser never marked final are still what was said; late ones are not asked again.
    assert stuck["log"]["done"] == [{"press": False, "heard": "publish it", "code": ""}]
    abandoned = listened["abandoned"]
    assert abandoned["calls"] == ["start", "abort"]
    assert abandoned["log"]["done"] == [] and abandoned["log"]["states"] == [True, False, True]
    assert abandoned["restarted"] is True


@needs_node
def test_the_voice_is_the_exact_tag_then_the_default_english_then_any_english(node_result):
    assert node_result["voices"] == ["au", "def", "gb", None, None]


@needs_node
def test_the_environment_label_is_a_short_word_or_nothing(node_result):
    assert node_result["environments"] == ["dev", "", "", ""]


def test_a_429_shows_the_apis_own_words_the_daily_cap_or_a_plain_throttle_message():
    """POST /ask answers 429 with the daily cap's words (ops_agent/quota.py); API Gateway's own
    throttle answers 429 with none of its own. Both are shown, never as "could not answer"."""
    code = _code(_read("ask.js"))
    ask = code[code.index("function ask(text)") :]

    status_429 = ask.index("response.status === 429")
    assert status_429 < ask.index("if (!response.ok)")
    block = ask[status_429 : ask.index("response.status === 400")]
    assert "errorFrom(response)" in block and "Too many questions just now" in block
