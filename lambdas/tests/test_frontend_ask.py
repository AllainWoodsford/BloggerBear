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
    assert 'redirectUri: "${local.site_url}/ask.html"' in config
    assert 'callback_urls = ["${local.site_url}/ask.html"]' in text
    assert 'logout_urls   = ["${local.site_url}/ask.html"]' in text


def test_production_gives_the_page_no_settings():
    text = (INFRA / "environments" / "production" / "main.tf").read_text(encoding="utf-8")

    assert "OPS_ASSISTANT" not in text
    assert "ops_assistant" not in text  # the day this fails, the assertions below need a decision
    assert "allow_microphone" not in text
    assert "hosted_ui_domain" not in text


def test_only_dev_loosens_the_sites_headers_and_only_for_what_the_page_needs():
    module = (INFRA / "modules" / "static-site" / "main.tf").read_text(encoding="utf-8")
    variables = (INFRA / "modules" / "static-site" / "variables.tf").read_text(encoding="utf-8")
    dev = (INFRA / "environments" / "dev" / "main.tf").read_text(encoding="utf-8")

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
    # The only two requests: the token exchange and the question.
    assert len(re.findall(r"\.fetch\(", code)) == 2
    assert '"/oauth2/token"' in code and ".fetch(config.askUrl" in code


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
    assert {key for _, key in uses} == {"VERIFIER_KEY", "STATE_KEY"}
    assert {method for method, _ in uses} == {"setItem", "getItem", "removeItem"}
    assert "sessionStorage[" not in code
    # Removed as soon as they are read, before the state is checked or the code is exchanged.
    start = code.index("function start()")
    removed = code.index("sessionStorage.removeItem(STATE_KEY)", start)
    assert removed < code.index("returnedState !== expectedState", start) < code.index("/oauth2/token", start)
    # The token is a variable: nothing named like one is ever handed to storage.
    assert not re.search(r"setItem\([^)]*(token|Token)", code)


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
    assert node_result["config"] == _CONFIG
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
