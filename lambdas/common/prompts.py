"""Prompts as assets: a prompt's text read from S3 when it may be, the copy in the code when not.

docs/enhancements/model-ranch-prompt-assets-evals.md (sections 4.1, 4.2 and 4.5). A prompt is
named `<lambda_id>/<prompt_name>` and has two homes: the registry (common/prompts_registry.py), a
`PromptSpec` per prompt with the code's own template, the **vanilla**, and the S3 bucket, where the
publisher workflow puts the asset files under `<prompt_id>/latest/` (one file: the prompt; two or
more: an A/B split, one arm per file). A call site renders through `get_prompt`, which serves an
asset when every check passes and the vanilla otherwise, and says which in the `Prompt` it returns
(`source`, `version`, `fallback_code`) so lineage can record what the model was given.

**The grammar.** A template holds `{{name}}` placeholders, filled from the call site's `values`,
and `{{locked:name}}` blocks, filled from its `locked` dict: guardrails the asset can place but not
change. The form is strict (lower-case letters and underscores, no spaces), so anything else in
the text, `{"verdicts"` in the reviewer prompts included, is literal. Rendering is one pass over
the template and never re-scans a value: a draft that contains `{{x}}` stays `{{x}}`. A template
is accepted only when every required placeholder is present, every placeholder is one the spec
names, every locked block appears exactly once, in the spec's order, and nothing but the locked
blocks (and whitespace) follows the first of them, so the guardrails always end the prompt; the
rendered text must fit `max_chars`. The vanilla is held to the same rules when its spec is
built, and its pinned sha256 is checked then, so a registry entry cannot drift unnoticed.

**The checks on an asset,** in order, each with the fallback code it fails with: the deployment's
switch (`assets_off`: PROMPT_AS_ASSET_ENABLED, with the bucket and hash table named), the
`prompts` config row's kill switch (`assets_off`, see below), the `latest/` listing (`s3_error`,
`no_files`), the arm's `version` object tag (`s3_error`, `no_version_tag`), the pause list
(`paused`), the body (`s3_error`, `decode_error`), the hash table (`hash_lookup_failed`,
`hash_unapproved`: the row for the file's sha256 must say `live`, or `retired` within
RETIRE_GRACE_S), the grammar (`missing_placeholder`, `unknown_placeholder`, `locked_missing`,
`locked_order`) and the size (`too_large`). Every fallback but the deployment switch's writes one
`prompt_fallback` evidence line (common/evidence.py; the observability module alarms on it): a
deployment that has not turned assets on has nothing to fall back from, and its alarm stays quiet.

**The arm.** With n files, the arm is `sha256("<run key>|<prompt_id>") % n` over the sorted keys,
so every prompt in one run lands on the same side of its own split (and different prompts on
independent sides), and the split is even across runs. The run key is bound with `bind_run` at the
run's boundary (the daily cycle's execution id, a rewrite id, a request id); unbound, the arm is
random.

**The cache.** Everything read is kept for CACHE_TTL_S in the container: the listing, the tags,
bodies by sha256, hash rows and the `prompts` row, so a warm container costs no more than one
round of reads every five minutes per prompt, and a superseded or paused version is served for at
most the TTL plus the retire grace (600 s). `get_prompt` never raises over anything about the
asset: every read is guarded and every failure is a vanilla with its code. What it does not
guard is a mistake at the call site that would break the vanilla as well (an id the registry does
not have, a value that cannot be rendered); the tests catch those.

**The `prompts` row** (`config_id = "prompts"` in the model-config table, written by the admin
API): `prompt_assets` (`on`|`off`) turns every asset off without a deploy, and `paused_versions`
(`<prompt_id>@<version>` entries) pulls one version while the others of its split keep serving.
An unreadable row falls open to the defaults: the hash check reads the same service and fails the
asset to vanilla when DynamoDB is down, so nothing unapproved is ever served.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3

from common import dynamo, evidence

CONFIG_ID = "prompts"
# Every setting of the `prompts` row and its default; an unset or invalid value takes the default.
SETTINGS: dict[str, object] = {"prompt_assets": "on", "paused_versions": []}
# How long one container keeps what it read, and how long a retired hash row is still accepted
# after `retired_at`, so a release does not fail the runs it overlaps.
CACHE_TTL_S = 300
RETIRE_GRACE_S = 300
# Every reason a vanilla is served instead of an asset (`Prompt.fallback_code`), and the `code` of
# the prompt_fallback evidence line.
FALLBACK_CODES = (
    "s3_error",
    "no_files",
    "no_version_tag",
    "too_large",
    "missing_placeholder",
    "unknown_placeholder",
    "locked_missing",
    "locked_order",
    "hash_unapproved",
    "hash_lookup_failed",
    "decode_error",
    "paused",
    "assets_off",
)
SOURCE_ASSET, SOURCE_VANILLA = "asset", "vanilla"

# The grammar: group 1 is the `locked:` prefix (or None), group 2 the name. Strict on purpose:
# `{{ name }}`, `{{Name}}`, `{{name1}}` and `{"verdicts"` are text.
PLACEHOLDER = re.compile(r"\{\{(locked:)?([a-z_]+)\}\}")
_LOCKED_PLACEHOLDER = re.compile(r"\{\{locked:[a-z_]+\}\}")
_NAME = re.compile(r"^[a-z_]+$")
# A version is the asset file's name without `.txt`: v1, v2, ... (the S3 `version` tag carries it).
_VERSION = re.compile(r"^v[1-9][0-9]*$")
_CODE = r"[a-z][a-z0-9_]{0,39}"
# A prompt id is two evidence codes (the Lambda, the prompt), so the fallback line can carry both.
_PROMPT_ID = re.compile(rf"^{_CODE}/{_CODE}$")
_PAUSED_ENTRY = re.compile(rf"^{_CODE}/{_CODE}@v[1-9][0-9]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PromptSpec:
    """One prompt as the code knows it: its id, the vanilla template with its version and pinned
    sha256, the placeholders it takes (`required` and `optional` values, `locked` blocks in the
    order they must appear), how long the rendered text may be, and whether it may ever be read
    from S3 (`asset=False`: the code-only prompts, served as `vanilla:<version>` always)."""

    prompt_id: str
    vanilla: str
    vanilla_version: str
    vanilla_sha256: str
    required: frozenset[str]
    optional: frozenset[str]
    locked: tuple[str, ...]
    max_chars: int
    asset: bool = True

    def __post_init__(self):
        object.__setattr__(self, "required", frozenset(self.required))
        object.__setattr__(self, "optional", frozenset(self.optional))
        object.__setattr__(self, "locked", tuple(self.locked))
        if not isinstance(self.prompt_id, str) or not _PROMPT_ID.match(self.prompt_id):
            raise ValueError(f"prompt_id {self.prompt_id!r} is not <lambda_id>/<prompt_name>")
        if not isinstance(self.vanilla_version, str) or not _VERSION.match(self.vanilla_version):
            raise ValueError(f"{self.prompt_id}: vanilla_version {self.vanilla_version!r} is not v1, v2, ...")
        if not isinstance(self.max_chars, int) or isinstance(self.max_chars, bool) or self.max_chars <= 0:
            raise ValueError(f"{self.prompt_id}: max_chars must be a positive whole number")
        names = [*self.required, *self.optional, *self.locked]
        if len(set(names)) != len(names):
            raise ValueError(
                f"{self.prompt_id}: a placeholder is named in more than one of required, optional, locked"
            )
        for name in names:
            if not isinstance(name, str) or not _NAME.match(name):
                raise ValueError(
                    f"{self.prompt_id}: placeholder name {name!r} is not lower-case letters and underscores"
                )
        if not isinstance(self.vanilla, str):
            raise ValueError(f"{self.prompt_id}: the vanilla must be text")
        actual = sha256_text(self.vanilla)
        if actual != self.vanilla_sha256:
            raise ValueError(
                f"{self.prompt_id}: vanilla_sha256 is {self.vanilla_sha256!r} "
                f"but the vanilla hashes to {actual!r}"
            )
        code = validate(self, self.vanilla)
        if code is not None:
            raise ValueError(f"{self.prompt_id}: the vanilla fails its own template rules: {code}")

    @property
    def vanilla_label(self) -> str:
        """What lineage records when the vanilla is served: `vanilla:v1`."""
        return f"vanilla:{self.vanilla_version}"


@dataclass(frozen=True)
class Prompt:
    """What `get_prompt` returns: the rendered text, and where it came from. `version` is the
    asset's S3 tag (`v2`) or `vanilla:v1`; `fallback_code` is one of FALLBACK_CODES when a vanilla
    was served in an asset's place, None when the text is the asset or the prompt is code-only."""

    text: str
    prompt_id: str
    version: str
    source: str
    fallback_code: str | None = None


# --- the grammar ------------------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    """The hex digest of `text` as UTF-8 with `\\n` newlines: the publisher's hash of a file, the
    registry's pin of a vanilla, and the key of the hash table."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def validate(spec: PromptSpec, template: str) -> str | None:
    """Why `template` cannot stand in for `spec`'s prompt (a fallback code), or None when it can.
    The size rule is not here: it applies to the rendered text (`get_prompt`)."""
    values, locked_seen, tail_start = set(), [], None
    for match in PLACEHOLDER.finditer(template):
        is_locked, name = match.group(1) is not None, match.group(2)
        if is_locked:
            if name not in spec.locked:
                return "unknown_placeholder"
            locked_seen.append(name)
            if tail_start is None:
                tail_start = match.start()
        else:
            if name not in spec.required and name not in spec.optional:
                return "unknown_placeholder"
            values.add(name)
    if not spec.required <= values:
        return "missing_placeholder"
    if set(spec.locked) - set(locked_seen):
        return "locked_missing"
    if locked_seen != list(spec.locked):
        return "locked_order"  # a block twice, or not in the spec's order
    # The tail: from the first locked block on, nothing but locked blocks and whitespace, so no text
    # of the asset's (and no value's) can follow the guardrails.
    if tail_start is not None and _LOCKED_PLACEHOLDER.sub("", template[tail_start:]).strip():
        return "locked_order"
    return None


def render(spec: PromptSpec, template: str, values: dict, locked: dict) -> str:
    """`template` with its placeholders filled, in one pass: a value's text is never scanned for
    placeholders of its own. A value that is missing or None renders as nothing (an optional block's
    empty form; a locked block may legally be empty too)."""

    def fill(match: re.Match) -> str:
        source = locked if match.group(1) is not None else values
        value = source.get(match.group(2))
        return "" if value is None else str(value)

    return PLACEHOLDER.sub(fill, template)


# --- the run and the arm -----------------------------------------------------------------------------

_run_key: contextvars.ContextVar[str | None] = contextvars.ContextVar("prompts_run_key", default=None)


@contextlib.contextmanager
def bind_run(key: str | None):
    """The run every `get_prompt` inside this block belongs to (the daily cycle's execution id, a
    rewrite id, a request id): one draw per run. Reset on exit, like evidence.bind, so a warm
    container never carries one run's key into the next."""
    token = _run_key.set(key)
    try:
        yield
    finally:
        _run_key.reset(token)


def bound_run() -> str | None:
    """The run key bound by `bind_run`, or None."""
    return _run_key.get()


def arm_index(prompt_id: str, n: int, run_key: str | None) -> int:
    """Which of `n` sorted files a run gets: the salted hash of the run key, so a run always lands
    on the same arm of one prompt's split and on independent arms of different prompts'; random
    when no run is bound."""
    if n <= 1:
        return 0
    if run_key is None:
        return random.randrange(n)
    digest = hashlib.sha256(f"{run_key}|{prompt_id}".encode()).hexdigest()
    return int(digest, 16) % n


# --- the registry ------------------------------------------------------------------------------------


def spec_for(prompt_id: str) -> PromptSpec:
    """The registry's spec for `prompt_id`. KeyError for an id the registry does not have: a mistake
    at the call site, with no vanilla to serve in its place."""
    from common import prompts_registry  # here, not at the top: the registry imports PromptSpec from here

    for spec in prompts_registry.all_specs():
        if spec.prompt_id == prompt_id:
            return spec
    raise KeyError(prompt_id)


def vanilla_prompt(prompt_id: str) -> str:
    """The code's own template for `prompt_id`, as it is (placeholders unfilled)."""
    return spec_for(prompt_id).vanilla


# --- the settings row -------------------------------------------------------------------------------


def validate_setting(name: str, value) -> str | None:
    """Why `value` is not a valid `name` on the `prompts` row, or None when it is (None, which
    clears a setting, is always valid)."""
    if value is None:
        return None
    if name not in SETTINGS:
        return f"unknown setting '{name}'"
    if name == "prompt_assets":
        return None if value in ("on", "off") else "'prompt_assets' must be on or off"
    if isinstance(value, list) and all(isinstance(e, str) and _PAUSED_ENTRY.match(e) for e in value):
        return None
    return "'paused_versions' must be a list of <lambda_id>/<prompt_name>@<version> entries"


def effective_settings(row: dict | None) -> dict:
    """Every setting's value in effect: the row's where it is valid, else the default."""
    row = row or {}
    settings = {}
    for name, default in SETTINGS.items():
        value = row.get(name)
        if value is not None and validate_setting(name, value) is not None:
            print(f"prompts: ignoring invalid {name}={value!r}; using {default!r}")
            value = None
        settings[name] = default if value is None else value
    return settings


def assets_enabled() -> bool:
    """Whether this deployment reads prompt assets at all: PROMPT_AS_ASSET_ENABLED is on and the
    bucket and the hash table are named (both roots set the three together)."""
    return (
        os.environ.get("PROMPT_AS_ASSET_ENABLED", "").strip().lower() == "true"
        and bool(os.environ.get("PROMPTS_BUCKET"))
        and bool(os.environ.get("PROMPT_HASHES_TABLE"))
    )


# --- reading ----------------------------------------------------------------------------------------

_s3_client = None
# (kind, ...) -> (expires at, value); see _cached.
_cache: dict[tuple, tuple[float, object]] = {}


def _now() -> float:
    """Seconds since the epoch, for the cache's expiry and the retire grace (one clock, so a test
    can move both)."""
    return time.time()


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _reset() -> None:
    """Forget the client and everything cached (tests, between cases)."""
    global _s3_client
    _s3_client = None
    _cache.clear()


def _cache_get(key: tuple):
    hit = _cache.get(key)
    if hit is None or hit[0] <= _now():
        return None
    return hit[1]


def _cache_put(key: tuple, value) -> None:
    _cache[key] = (_now() + CACHE_TTL_S, value)


def _cached(key: tuple, load):
    """The cached value for `key`, or `load()`'s, kept for the TTL. A `load` that raises caches
    nothing, so a failed read is tried again on the next call."""
    hit = _cache.get(key)
    if hit is not None and hit[0] > _now():
        return hit[1]
    value = load()
    _cache_put(key, value)
    return value


def _settings() -> dict:
    try:
        return _cached(("settings",), lambda: effective_settings(dynamo.get_config_row(CONFIG_ID)))
    except Exception as exc:  # noqa: BLE001 - the row is advisory; the hash check is the gate
        print(f"prompts: could not read the {CONFIG_ID} settings row; using defaults: {exc!r}")
        return effective_settings(None)


def _list_latest(prompt_id: str) -> list[str]:
    """The keys under `<prompt_id>/latest/`, sorted (the arm's order), folder markers left out."""
    bucket, prefix = os.environ["PROMPTS_BUCKET"], f"{prompt_id}/latest/"
    keys = []
    for page in _s3().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [item["Key"] for item in page.get("Contents", []) if not item["Key"].endswith("/")]
    return sorted(keys)


def _tags(key: str) -> dict[str, str]:
    response = _s3().get_object_tagging(Bucket=os.environ["PROMPTS_BUCKET"], Key=key)
    return {tag["Key"]: tag["Value"] for tag in response.get("TagSet", [])}


def _object(key: str, sha_hint: str | None) -> tuple[str, str]:
    """(sha256, text) of the object at `key`: its sha cached by key and its body by sha, both for
    the TTL, so the same bytes under two keys are read once. `sha_hint` is the publisher's `sha256`
    tag, a way into the body cache only: a body is cached under the sha of the bytes actually read,
    so what comes back has the sha it is served under whatever the tag says. UnicodeDecodeError for
    a body that is not UTF-8."""
    sha = _cache_get(("object", key)) or sha_hint
    if sha is not None:
        text = _cache_get(("body", sha))
        if text is not None:
            return sha, text
    raw = _s3().get_object(Bucket=os.environ["PROMPTS_BUCKET"], Key=key)["Body"].read()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    sha = sha256_text(text)
    _cache_put(("object", key), sha)
    _cache_put(("body", sha), text)
    return sha, text


def _hash_row(sha: str) -> dict | None:
    table = dynamo.get_table(os.environ["PROMPT_HASHES_TABLE"])
    return table.get_item(Key={"prompt_hash": sha}, ConsistentRead=True).get("Item")


def _epoch(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def _approved(row: dict | None) -> bool:
    """Whether the hash row lets the file be served: `live`, or `retired` no more than
    RETIRE_GRACE_S ago. No row, `pending` (a publish under way) or anything else: no."""
    if not row:
        return False
    if row.get("status") == "live":
        return True
    if row.get("status") == "retired":
        retired = _epoch(row.get("retired_at"))
        return retired is not None and _now() - retired <= RETIRE_GRACE_S
    return False


def _read_asset(spec: PromptSpec) -> tuple[str | None, str | None, str | None]:
    """The arm's template for this run: (template, version, None), or (None, version or None, the
    fallback code) at the first check that fails. Every read is guarded here."""
    settings = _settings()
    if settings["prompt_assets"] == "off":
        return None, None, "assets_off"
    try:
        keys = _cached(("listing", spec.prompt_id), lambda: _list_latest(spec.prompt_id))
    except Exception as exc:  # noqa: BLE001 - any failure to read is a fallback, never an error
        print(f"prompts: {spec.prompt_id}: could not list latest/: {exc!r}")
        return None, None, "s3_error"
    if not keys:
        return None, None, "no_files"
    key = keys[arm_index(spec.prompt_id, len(keys), bound_run())]
    try:
        tags = _cached(("tags", key), lambda: _tags(key))
    except Exception as exc:  # noqa: BLE001
        print(f"prompts: {spec.prompt_id}: could not read the tags of {key}: {exc!r}")
        return None, None, "s3_error"
    version = tags.get("version")
    if not isinstance(version, str) or not _VERSION.match(version):
        return None, None, "no_version_tag"
    if f"{spec.prompt_id}@{version}" in settings["paused_versions"]:
        return None, version, "paused"
    sha_hint = tags.get("sha256")
    if not isinstance(sha_hint, str) or not _SHA256.match(sha_hint):
        sha_hint = None
    try:
        sha, template = _object(key, sha_hint)
    except UnicodeDecodeError:
        return None, version, "decode_error"
    except Exception as exc:  # noqa: BLE001
        print(f"prompts: {spec.prompt_id}: could not read {key}: {exc!r}")
        return None, version, "s3_error"
    try:
        row = _cached(("hash", sha), lambda: _hash_row(sha))
    except Exception as exc:  # noqa: BLE001
        print(f"prompts: {spec.prompt_id}: could not read the hash row: {exc!r}")
        return None, version, "hash_lookup_failed"
    if not _approved(row):
        return None, version, "hash_unapproved"
    code = validate(spec, template)
    if code is not None:
        return None, version, code
    return template, version, None


def _vanilla(spec: PromptSpec, values: dict, locked: dict, code: str | None) -> Prompt:
    text = render(spec, spec.vanilla, values, locked)
    return Prompt(text, spec.prompt_id, spec.vanilla_label, SOURCE_VANILLA, code)


def _fallback(spec: PromptSpec, values: dict, locked: dict, code: str, version: str | None) -> Prompt:
    """The vanilla in an asset's place, and the evidence line that says why (the asset's version
    when one was read)."""
    lambda_id, _, prompt_name = spec.prompt_id.partition("/")
    evidence.emit("prompt_fallback", lambda_id=lambda_id, prompt_name=prompt_name, version=version, code=code)
    return _vanilla(spec, values, locked, code)


def get_prompt(prompt_id: str, values: dict | None = None, locked: dict | None = None) -> Prompt:
    """The prompt to send: the run's arm of the asset when the deployment reads assets and every
    check passes, else the vanilla with the code that says why (FALLBACK_CODES). A code-only spec
    (`asset=False`) is the vanilla, with no code and no read of S3 or DynamoDB; so is every prompt
    while the deployment's switch is off. Never raises over the asset: see the module docstring."""
    spec = spec_for(prompt_id)
    values, locked = values or {}, locked or {}
    if not spec.asset:
        return _vanilla(spec, values, locked, None)
    if not assets_enabled():
        return _vanilla(spec, values, locked, "assets_off")
    template, version, code = _read_asset(spec)
    if code is None:
        text = render(spec, template, values, locked)
        if len(text) <= spec.max_chars:
            return Prompt(text, prompt_id, version, SOURCE_ASSET, None)
        code = "too_large"
    return _fallback(spec, values, locked, code, version)
