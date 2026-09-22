"""The review inbox: what is waiting for you, and a fast way to clear it.

Two commands (wired into admin_cli.py):

    python scripts/admin_cli.py inbox            # what needs you, at a glance
    python scripts/admin_cli.py approve          # go through it, one keystroke each
    python scripts/admin_cli.py approve --mock   # practise with made-up items, no AWS

What needs a person, in BloggerBear:

* Articles in the moderation queue. Financial topics (crypto, the trending digest) always wait
  for you by design. Others land here when the compliance or fresh-data review held them, and
  the reasons are shown.
* Prompt-change proposals from the weekly reflection. Approving one changes how future articles
  on that topic are written.

Everything goes through the Admin API, exactly like every other admin_cli command, never straight to
DynamoDB or S3: approving an article does more than flip a flag (it renders the page, publishes it,
writes a musing), and the API is where that lives. The keys are y (approve), r (reject), z (skip),
plus v (read the whole thing) and q (quit). Each choice is applied at once, so quitting, an error or a
dropped connection never loses progress.

A skipped item is left exactly as it is, and hidden from your next reviews for a while (a small local
file, never sent anywhere) so a rerun gives you the next batch instead of the same ones.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_LIMIT = 30
DEFAULT_SKIP_HOURS = 24
PREVIEW_CHARS = 700
# Compliance's fixed reason for a financial topic (lambdas/common/compliance.py): routine, by design.
_FINANCIAL_REASON = "financial topic"

HELP_LINE = "[y] Approve | [r] Reject | [z] Skip | [v] View all | [q] Quit -> "


class ApiError(Exception):
    """The Admin API could not do what was asked. `status` is the HTTP status (0 = the network)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Api:
    """A thin wrapper over admin_cli's signed request, so errors come back as ApiError.

    `request(method, path, body=None)` returns an object with `status_code`, `json()` and `text`
    (a `requests` response); it is injected so tests and mock mode need no network.
    """

    def __init__(self, request: Callable):
        self._request = request

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            response = self._request(method, path, body)
        except OSError as exc:  # ConnectionError, Timeout and friends are all OSErrors
            raise ApiError(0, f"network problem ({type(exc).__name__})") from exc
        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if 200 <= status < 300:
            return payload if isinstance(payload, dict) else {}
        message = payload.get("error") if isinstance(payload, dict) else None
        if status == 403:
            message = message or (
                "not allowed (is your IP on the admin allowlist? are your AWS credentials right?)"
            )
        raise ApiError(status, message or f"the API answered {status}")

    def get(self, path: str) -> dict:
        return self._call("GET", path)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._call("POST", path, body)

    def delete(self, path: str) -> dict:
        return self._call("DELETE", path)


@dataclass
class Item:
    """One thing waiting for a decision, in the same shape whatever it came from."""

    source: str  # "moderation", "refinements" or "mock"
    key: str  # unique within its source
    title: str
    topic: str | None = None
    created_at: str | None = None
    why: list[str] = field(default_factory=list)  # why it needs a person
    notes: list[str] = field(default_factory=list)  # review notes worth a look
    facts: list[str] = field(default_factory=list)  # cost, sources, ...
    body: str = ""
    routine: bool = False  # waits for a person by design (a financial topic), nothing wrong
    caution: bool = False  # held for a reason a person should look at before approving
    approve_note: str = ""  # what approving does, when that is not obvious
    ref: dict = field(default_factory=dict)  # what the source needs to act on it

    @property
    def skip_key(self) -> str:
        return f"{self.source}:{self.key}"

    @property
    def kind(self) -> str:
        """What to call it on the card."""
        return {"moderation": "ARTICLE", "refinements": "PROMPT CHANGE", "mock": "PRACTICE"}.get(
            self.source, self.source.upper()
        )


class ContentSource(ABC):
    """Something that has items waiting for a decision. Add a new kind of waiting thing by
    subclassing this and listing it in `build_sources`."""

    name = ""
    label = ""

    @abstractmethod
    def fetch(self, limit: int, exclude: set[str]) -> list[Item]:
        """Up to `limit` items, oldest first, leaving out any whose skip_key is in `exclude`."""

    @abstractmethod
    def approve(self, item: Item) -> str:
        """Approve `item`. Returns a short message about what happened."""

    @abstractmethod
    def reject(self, item: Item) -> str:
        """Reject `item`. Returns a short message about what happened."""

    def prepare_approve(self, item: Item, key_reader: Callable[[], str], out) -> bool:
        """Ask whatever must be decided before approving (nothing, by default). False means the
        person backed out: stay on the item. Called only for a real approval, never a dry run."""
        return True


def _oldest_first(rows: list[dict], date_field: str) -> list[dict]:
    return sorted(rows, key=lambda row: row.get(date_field) or "")


class ModerationSource(ContentSource):
    """Articles waiting in the moderation queue."""

    name = "moderation"
    label = "articles"

    def __init__(self, api: Api):
        self.api = api

    def fetch(self, limit: int, exclude: set[str]) -> list[Item]:
        rows = self.api.get("/moderation-queue").get("items") or []
        wanted = [
            row
            for row in _oldest_first(rows, "created_at")
            if f"{self.name}:{row.get('queue_id')}" not in exclude
        ][:limit]
        return [self._item(row) for row in wanted]

    def _item(self, row: dict) -> Item:
        reasons = [str(r) for r in row.get("reasons") or []]
        routine = [r for r in reasons if _FINANCIAL_REASON in r.lower()]
        flagged = [r for r in reasons if r not in routine]
        notes = [str(n) for n in row.get("review_notes") or []]
        item = Item(
            source=self.name,
            key=str(row.get("queue_id")),
            title="(article unavailable)",
            topic=row.get("topic_id"),
            created_at=row.get("created_at"),
            why=(["Financial topic: always reviewed by a person."] if routine else []) + flagged,
            notes=notes,
            routine=bool(routine) and not flagged and not notes,
            caution=bool(flagged or notes),
            ref={"queue_id": row.get("queue_id"), "article_id": row.get("article_id")},
        )
        try:
            article = self.api.get(f"/articles/{row.get('article_id')}")
        except ApiError as exc:
            item.notes.append(f"Could not load the article text: {exc.message}")
            item.caution = True
            return item
        item.title = article.get("title") or "(untitled)"
        item.body = article.get("body") or ""
        if article.get("body_error"):
            item.notes.append(article["body_error"])
            item.caution = True
        cost = article.get("cost_aud")
        item.facts = [
            f"Sources cited: {len(article.get('source_refs') or [])}",
            f"Cost to write: ~${cost:.3f} AUD" if isinstance(cost, int | float) else "Cost: no data",
        ]
        return item

    def approve(self, item: Item) -> str:
        self.api.post(f"/moderation-queue/{item.ref['queue_id']}/approve")
        return "published"

    def reject(self, item: Item) -> str:
        self.api.post(f"/moderation-queue/{item.ref['queue_id']}/reject")
        return "rejected (stays private)"


class RefinementSource(ContentSource):
    """Prompt-change proposals from the weekly reflection."""

    name = "refinements"
    label = "prompt changes"

    def __init__(self, api: Api):
        self.api = api

    def fetch(self, limit: int, exclude: set[str]) -> list[Item]:
        rows = self.api.get("/prompt-refinements?status=pending").get("refinements") or []
        items = []
        for row in _oldest_first(rows, "version"):
            key = f"{row.get('topic_id')}|{row.get('version')}"
            if f"{self.name}:{key}" in exclude:
                continue
            items.append(
                Item(
                    source=self.name,
                    key=key,
                    title=f"Prompt change for {row.get('topic_id')}",
                    topic=row.get("topic_id"),
                    created_at=row.get("version"),
                    why=[str(row.get("rationale") or "(no rationale given)")],
                    facts=_gear_facts(row),
                    body=str(row.get("prompt_changes") or ""),
                    approve_note="Approving changes how future articles on this topic are written.",
                    ref={
                        "topic_id": row.get("topic_id"),
                        "version": row.get("version"),
                        "slot_hint": row.get("slot_hint"),
                    },
                )
            )
            if len(items) >= limit:
                break
        return items

    def prepare_approve(self, item: Item, key_reader: Callable[[], str], out) -> bool:
        """Approving a prompt change also decides where the bear wears it, so ask."""
        try:
            loadout = self.api.get("/equipment")
        except ApiError as exc:
            _emit(f"  ! Could not read what the bear is wearing: {exc.message}", out)
            return False
        placement = choose_placement(item, loadout, key_reader, out)
        if placement is None:
            return False
        item.ref["placement"] = placement
        return True

    def approve(self, item: Item) -> str:
        path = f"/prompt-refinements/{item.ref['topic_id']}/{item.ref['version']}/approve"
        response = self.api.post(path, item.ref.get("placement") or None)
        approved = response.get("approved") or {}
        found = _gear_line(approved.get("item"))
        return "approved: " + found + _worn_message(approved.get("placement") or {})

    def reject(self, item: Item) -> str:
        self.api.post(f"/prompt-refinements/{item.ref['topic_id']}/{item.ref['version']}/reject")
        return "rejected"


def _gear_line(gear: dict | None) -> str:
    """"Helm of Plain Speaking (rare, durability 17/17): " -- what the bear found, when the API says."""
    if not gear or not gear.get("name"):
        return ""
    wear = f"durability {gear.get('durability')}/{gear.get('max_durability')}"
    return f"{gear['name']} ({gear.get('rarity')}, {wear}): "


def _gear_facts(row: dict) -> list[str]:
    """What the bear found in a proposal, for its card. Nothing for one proposed before gear."""
    if not row.get("rarity"):
        return []
    facts = [
        f"The bear found: {row.get('name')} ({row.get('rarity')})",
        f"Durability: {row.get('durability')}/{row.get('max_durability')}",
    ]
    if row.get("slot_hint"):
        facts.append(f"The bear suggests: {row['slot_hint']}")
    return facts


def _short(text, limit: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _worn_message(placement: dict) -> str:
    """What became of an approved prompt change, in words."""
    if not placement.get("equipped"):
        return "it is in the backpack, not worn, so future drafts will not use it yet"
    slot = placement.get("slot")
    if slot == "ring":
        where = "as a ring for its topic"
    else:
        where = f"as {placement.get('scope')} guidance in the {slot} slot"
    displaced = placement.get("displaced")
    replaced = f" (replacing {displaced.get('topic_id')} {displaced.get('version')})" if displaced else ""
    return f"worn {where}{replaced}; future drafts will use it"


def _ask(key_reader: Callable[[], str], out, prompt: str, valid: str, default: str | None = None):
    """One keystroke out of `valid`. Enter takes `default` if there is one; c (or Ctrl-C, or the
    end of input) backs out and returns None."""
    while True:
        print(prompt, end="", file=out, flush=True)
        try:
            key = key_reader()
        except KeyboardInterrupt:
            key = "c"
        print(key.strip() if key else "", file=out, flush=True)
        if key in ("\r", "\n", " ") and default:
            return default
        if key in ("c", "q"):
            return None
        if key and key in valid:
            return key
        _emit(f"  Press one of: {', '.join(valid)}, or c to cancel.", out)


def choose_placement(item: Item, loadout: dict, key_reader: Callable[[], str], out) -> dict | None:
    """Ask where an approved prompt change goes. Returns the body for the approve call, or None
    when the person backs out. `loadout` is GET /equipment. It says what would be replaced before
    anything is."""
    armor = loadout.get("armor") or {}
    rings = loadout.get("rings") or []
    max_rings = int(loadout.get("max_rings") or 5)
    suggested = item.ref.get("slot_hint")
    armor_suggested = suggested in armor  # the bear thinks it is armor; anything else, a ring
    _emit("  Where should the bear wear it?", out)
    _emit(
        f"    t  a ring for {item.topic} ({len(rings)} of {max_rings} rings worn)"
        + ("" if armor_suggested else "  [Enter]"),
        out,
    )
    _emit(
        "    g  an armor slot: guidance for every topic"
        + (f"; the bear suggests the {suggested}  [Enter]" if armor_suggested else ""),
        out,
    )
    _emit(f"    b  the backpack: approved but not worn ({loadout.get('backpack_count', 0)} there now)", out)
    choice = _ask(
        key_reader, out, "  Choose t / g / b (c cancels): ", "tgb", default="g" if armor_suggested else "t"
    )
    if choice is None:
        return None
    if choice == "b":
        return {"scope": "backpack"}
    if choice == "t":
        if len(rings) < max_rings:
            return {"scope": "topic"}
        _emit("  Every ring is worn. Replace which one?", out)
        for number, ring in enumerate(rings, start=1):
            _emit(f"    {number}  {ring.get('topic_id')}: {_short(ring.get('prompt_changes'))}", out)
        numbers = "".join(str(n) for n in range(1, len(rings) + 1))
        pick = _ask(key_reader, out, "  Replace ring number (c cancels): ", numbers)
        if pick is None:
            return None
        ring = rings[int(pick) - 1]
        return {"scope": "topic", "replace": {"topic_id": ring["topic_id"], "version": ring["version"]}}
    slots = list(armor)
    first_empty = next((n for n, slot in enumerate(slots, start=1) if not armor[slot]), None)
    # The bear's pick if that slot is empty, else the first empty one, else nothing is offered.
    empty = slots.index(suggested) + 1 if armor_suggested and not armor[suggested] else first_empty
    hint = "  (Enter takes the empty one marked *)" if empty else "  (all are worn)"
    _emit("  Which armor slot?" + hint, out)
    for number, slot in enumerate(slots, start=1):
        held = armor[slot]
        state = "empty"
        if held:
            state = f"worn: {_short(held.get('prompt_changes'), 45)}  <- it would be replaced"
        _emit(f"    {number}  {slot}: {state}" + ("  *" if number == empty else ""), out)
    pick = _ask(
        key_reader,
        out,
        "  Slot number (c cancels): ",
        "".join(str(n) for n in range(1, len(slots) + 1)),
        default=str(empty) if empty else None,
    )
    if pick is None:
        return None
    return {"scope": "global", "slot": slots[int(pick) - 1]}


class MockSource(ContentSource):
    """Made-up items, for practising the keys with no AWS and no network. Nothing is sent anywhere."""

    name = "mock"
    label = "practice items"

    def __init__(self, count: int = 8, now: datetime | None = None):
        now = now or datetime.now(UTC)
        self.log: list[tuple[str, str]] = []
        self._items = []
        samples = [
            ("Bitcoin Holds Its Range as Ethereum Slips", "finance-crypto-investing", True, False),
            ("Trending This Week: Agents, Crypto and Policy", "digest", True, False),
            ("Cloudflare's Auditor Rides the Wave", "github-trending", False, True),
            ("A Quiet Week for Security Headlines", "security-hacker-news", False, False),
        ]
        for n in range(count):
            title, topic, routine, caution = samples[n % len(samples)]
            self._items.append(
                Item(
                    source=self.name,
                    key=f"mock-{n + 1}",
                    title=f"{title} (practice #{n + 1})",
                    topic=topic,
                    created_at=(now - timedelta(hours=3 * (count - n))).isoformat(),
                    why=(["Financial topic: always reviewed by a person."] if routine else [])
                    + (["Fabricated claim: \"raised a $50 million round\""] if caution else []),
                    notes=["Bitcoin is quoted at $81,000 but the source now says $79,400."]
                    if caution
                    else [],
                    facts=["Sources cited: 3", "Cost to write: ~$0.012 AUD"],
                    body=(
                        "This is a practice article. " * 12
                        + "\n\nIt has a second paragraph, so you can see how a longer text looks "
                        + "when you press v to read all of it. " * 6
                    ),
                    routine=routine and not caution,
                    caution=caution,
                )
            )

    def fetch(self, limit: int, exclude: set[str]) -> list[Item]:
        return [item for item in self._items if item.skip_key not in exclude][:limit]

    def approve(self, item: Item) -> str:
        self.log.append(("approve", item.key))
        return "approved (practice: nothing was sent)"

    def reject(self, item: Item) -> str:
        self.log.append(("reject", item.key))
        return "rejected (practice: nothing was sent)"


class SkipStore:
    """Remembers what you skipped, in a small local file, so a rerun moves on to the next batch.

    Never sent anywhere and never changes anything in AWS: a skipped item is still exactly as it was.
    Skips lapse after `hours` (0 means they never do until you clear them).
    """

    def __init__(
        self,
        path: Path | str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        default = os.environ.get("BLOGGERBEAR_REVIEW_STATE") or str(
            Path.home() / ".bloggerbear" / "review-skips.json"
        )
        self.path = Path(path or default)
        self._now = now
        self._skips: dict[str, str] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            skips = data.get("skipped") if isinstance(data, dict) else None
            if isinstance(skips, dict):
                self._skips = {str(k): str(v) for k, v in skips.items()}
        except (OSError, ValueError):
            pass  # no file yet, or one we cannot read: start empty

    def active(self, hours: int) -> set[str]:
        """The keys currently hidden."""
        if hours <= 0:
            return set(self._skips)
        cutoff = self._now() - timedelta(hours=hours)
        hidden = set()
        for key, stamp in self._skips.items():
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when >= cutoff:
                hidden.add(key)
        return hidden

    def add(self, key: str) -> None:
        self._skips[key] = self._now().isoformat()
        self.save()

    def forget(self, key: str) -> None:
        """An item you decided on is no longer waiting: drop its skip record."""
        if self._skips.pop(key, None) is not None:
            self.save()

    def clear(self) -> int:
        count = len(self._skips)
        self._skips = {}
        self.save()
        return count

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"skipped": self._skips}, indent=1), encoding="utf-8")
            temp.replace(self.path)
        except OSError as exc:
            print(f"(could not remember your skips: {exc})", file=sys.stderr)


# --- keys and text ----------------------------------------------------------------------------


def read_key() -> str:
    """One keystroke, without waiting for Enter, from a real terminal; a line of input otherwise
    (a pipe, a test). End of input counts as q, and Ctrl-C raises KeyboardInterrupt."""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            import msvcrt  # Windows

            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):  # an arrow or function key: ignore it
                msvcrt.getwch()
                return ""
        except ImportError:
            import termios
            import tty

            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                char = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\x04", ""):
            return "q"
        return char.lower()
    try:
        line = input()
    except EOFError:
        return "q"
    return line.strip()[:1].lower()


def _age(created_at: str | None, now: datetime) -> str:
    if not created_at:
        return "unknown age"
    try:
        when = datetime.fromisoformat(created_at)
    except ValueError:
        return str(created_at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 3600:
        ago = f"{max(1, seconds // 60)} min ago"
    elif seconds < 86400:
        ago = f"{seconds // 3600} h ago"
    else:
        ago = f"{seconds // 86400} d ago"
    return f"{ago} ({when.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} UTC)"


def _width() -> int:
    return max(60, min(100, shutil.get_terminal_size((90, 24)).columns - 2))


def _wrap(text: str, width: int, indent: str = "  ") -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(paragraph, width, initial_indent=indent, subsequent_indent=indent) or [""])
    return lines


def render_item(item: Item, index: int, total: int, now: datetime, width: int | None = None) -> str:
    """The card shown for one item: where it is from, what it is, why it needs you, and a preview."""
    width = width or _width()
    lines = ["", "=" * width, f"[{index}/{total}] {item.kind}", "-" * width]
    for chunk in textwrap.wrap(item.title, width - 2) or [""]:
        lines.append(chunk)
    lines.append(f"Topic: {item.topic or 'n/a'}   |   Waiting since {_age(item.created_at, now)}")
    for fact in item.facts:
        lines.append(f"  {fact}")
    if item.why:
        lines.append("")
        lines.append("Why it needs you:")
        for reason in item.why:
            lines.extend(_wrap("- " + reason, width, "  ")[:6])
    if item.notes:
        lines.append("Review notes (look before approving):")
        for note in item.notes:
            lines.extend(_wrap("! " + note, width, "  ")[:6])
    if item.approve_note:
        lines.append(f"Note: {item.approve_note}")
    lines.append("")
    preview = item.body.strip()
    if preview:
        shown = preview[:PREVIEW_CHARS].rstrip()
        lines.extend(_wrap(shown, width))
        if len(preview) > PREVIEW_CHARS:
            lines.append(f"  [... {len(preview) - PREVIEW_CHARS} more characters: press v to read all of it]")
    else:
        lines.append("  (no text to preview)")
    lines.append("-" * width)
    return "\n".join(lines)


# --- the review loop ------------------------------------------------------------------------------


@dataclass
class Summary:
    approved: int = 0
    rejected: int = 0
    skipped: int = 0
    errors: int = 0
    already_handled: int = 0
    fetched: int = 0
    quit_early: bool = False
    dry_run: bool = False

    def line(self) -> str:
        parts = f"{self.approved} approved, {self.rejected} rejected, {self.skipped} skipped"
        if self.errors:
            parts += f", {self.errors} failed"
        if self.already_handled:
            parts += f", {self.already_handled} already handled elsewhere"
        return parts + (" (dry run: nothing was changed)" if self.dry_run else "")


def _emit(text: str, out) -> None:
    """Print, surviving a console that cannot show a character in an article title."""
    try:
        print(text, file=out, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(out, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding), file=out, flush=True)


def fetch_batch(
    sources: list[ContentSource], limit: int, exclude: set[str], out=None
) -> tuple[list[tuple[ContentSource, Item]], list[str]]:
    """Up to `limit` items across `sources`, in order. A source that fails is reported, not fatal."""
    batch: list[tuple[ContentSource, Item]] = []
    problems: list[str] = []
    for source in sources:
        remaining = limit - len(batch)
        if remaining <= 0:
            break
        try:
            batch.extend((source, item) for item in source.fetch(remaining, exclude))
        except ApiError as exc:
            problems.append(f"Could not fetch {source.label}: {exc.message}")
    return batch, problems


def review(
    sources: list[ContentSource],
    *,
    limit: int = DEFAULT_LIMIT,
    store: SkipStore,
    skip_hours: int = DEFAULT_SKIP_HOURS,
    include_skipped: bool = False,
    dry_run: bool = False,
    key_reader: Callable[[], str] = read_key,
    out=None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Summary:
    """Go through what is waiting, one item at a time. Each choice is applied at once."""
    out = out or sys.stdout
    summary = Summary(dry_run=dry_run)
    exclude = set() if include_skipped else store.active(skip_hours)
    batch, problems = fetch_batch(sources, limit, exclude)
    for problem in problems:
        _emit(f"! {problem}", out)
    summary.fetched = len(batch)
    if not batch:
        hidden = " (Skipped items are hidden; --include-skipped shows them.)" if exclude else ""
        _emit("Nothing is waiting for you." + hidden, out)
        return summary

    dry_note = " (dry run: nothing will be changed)" if dry_run else ""
    _emit(f"{len(batch)} item(s) to review{dry_note}.", out)
    for number, (source, item) in enumerate(batch, start=1):
        _emit(render_item(item, number, len(batch), now()), out)
        while True:
            print(HELP_LINE, end="", file=out, flush=True)
            try:
                key = key_reader()
            except KeyboardInterrupt:
                key = "q"
            print(key or "", file=out, flush=True)
            if key == "v":
                _emit("\n".join(_wrap(item.body or "(no text)", _width())), out)
                continue
            if key == "q":
                summary.quit_early = True
                break
            if key == "z":
                summary.skipped += 1
                if not dry_run:
                    store.add(item.skip_key)
                break
            if key in ("y", "r"):
                if key == "y" and item.caution and not _confirmed(key_reader, out):
                    continue
                if key == "y" and not dry_run and not source.prepare_approve(item, key_reader, out):
                    continue
                if _apply(source, item, key, summary, store, dry_run, out):
                    break
                # a failure: stay on this item so you can retry it, skip it, or quit
                continue
            _emit("  Press y, r, z, v or q.", out)
        if summary.quit_early:
            break

    _emit("", out)
    _emit(summary.line() + ".", out)
    if summary.quit_early:
        _emit("You stopped early: nothing you already decided is lost.", out)
    elif summary.fetched >= limit:
        _emit("There may be more: run it again for the next batch.", out)
    return summary


def _confirmed(key_reader: Callable[[], str], out) -> bool:
    print("  It was held for a reason (see above). Approve anyway? [y/N] ", end="", file=out, flush=True)
    try:
        answer = key_reader()
    except KeyboardInterrupt:
        answer = "n"
    print(answer, file=out, flush=True)
    return answer == "y"


def _apply(source, item, key, summary, store, dry_run, out) -> bool:
    """Apply an approve/reject. True when it is settled (done, or already handled elsewhere)."""
    verb = "approve" if key == "y" else "reject"
    if dry_run:
        _emit(f"  (dry run) would {verb}.", out)
        summary.approved += key == "y"
        summary.rejected += key == "r"
        return True
    try:
        message = source.approve(item) if key == "y" else source.reject(item)
    except ApiError as exc:
        if exc.status == 409:  # someone (or another run) already dealt with it
            _emit("  Already handled elsewhere: nothing to do.", out)
            summary.already_handled += 1
            store.forget(item.skip_key)
            return True
        _emit(f"  ! Could not {verb} this one: {exc.message}", out)
        summary.errors += 1
        return False
    _emit(f"  OK: {message}", out)
    summary.approved += key == "y"
    summary.rejected += key == "r"
    store.forget(item.skip_key)
    return True


# --- the inbox summary -----------------------------------------------------------------------------


def inbox_report(api: Api, now: datetime | None = None) -> str:
    """What is waiting for you, at a glance. A part that cannot be checked says so, and the rest
    still shows."""
    now = now or datetime.now(UTC)
    lines = ["", "BloggerBear inbox", "=" * 40]

    try:
        rows = api.get("/moderation-queue").get("items") or []
        routine = held = 0
        oldest = None
        for row in rows:
            reasons = [str(r).lower() for r in row.get("reasons") or []]
            if reasons and all(_FINANCIAL_REASON in r for r in reasons) and not row.get("review_notes"):
                routine += 1
            else:
                held += 1
            oldest = min(filter(None, [oldest, row.get("created_at")]), default=oldest)
        lines.append(f"Articles waiting for you: {len(rows)}")
        if rows:
            lines.append(f"  {routine} on financial topics (always reviewed by a person)")
            lines.append(f"  {held} held by a review (look at these first: the reasons say why)")
            lines.append(f"  oldest: {_age(oldest, now)}")
    except ApiError as exc:
        lines.append(f"Articles waiting for you: could not check ({exc.message})")

    try:
        pending = api.get("/prompt-refinements?status=pending").get("refinements") or []
        lines.append(f"Prompt changes waiting for you: {len(pending)}")
    except ApiError as exc:
        lines.append(f"Prompt changes waiting for you: could not check ({exc.message})")

    try:
        failed = api.get("/failed-executions").get("items") or []
        lines.append(f"Failed runs (dead-letter queue): {len(failed)}")
        if failed:
            lines.append("  something did not finish: `admin_cli failed-executions list` shows what")
    except ApiError as exc:
        lines.append(f"Failed runs: could not check ({exc.message})")

    try:
        config = api.get("/feedback-config")
        effective = config.get("effective") or {}
        if effective.get("locked_down"):
            lines.append("Heads-up: feedback is LOCKED DOWN (readers see the reason).")
        if effective.get("verification_required") is False:
            lines.append("Heads-up: feedback verification is switched OFF.")
    except ApiError:
        pass  # a heads-up only

    lines += ["", "To go through them:  python scripts/admin_cli.py approve", ""]
    return "\n".join(lines)


def mock_inbox_report() -> str:
    return "\n".join(
        [
            "",
            "BloggerBear inbox (practice: made-up numbers)",
            "=" * 40,
            "Articles waiting for you: 6",
            "  4 on financial topics (always reviewed by a person)",
            "  2 held by a review (look at these first: the reasons say why)",
            "Prompt changes waiting for you: 1",
            "Failed runs (dead-letter queue): 0",
            "",
            "To go through them:  python scripts/admin_cli.py approve --mock",
            "",
        ]
    )


def build_sources(api: Api | None, which: str, mock: bool) -> list[ContentSource]:
    """The sources for a run. `which` is "all", "moderation" or "refinements"."""
    if mock:
        return [MockSource()]
    if api is None:
        raise ValueError("a live run needs the Admin API")
    sources: list[ContentSource] = []
    if which in ("all", "moderation"):
        sources.append(ModerationSource(api))
    if which in ("all", "refinements"):
        sources.append(RefinementSource(api))
    return sources
