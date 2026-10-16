"""The repository's Claude Code skills (.claude/skills/*/SKILL.md): each is a name and a description
and nothing that pre-approves a command, every Admin CLI command it shows is one the real parser
accepts and none of them deletes or forces, and nothing in it would fetch, upload or name a
deployment (an account id, an API host, the API URL on a command line)."""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest
import yaml

import admin_cli
import generate_cli_reference as generator

SKILLS = Path(__file__).resolve().parents[2] / ".claude" / "skills"
PROGRAM = "python scripts/admin_cli.py"
# What a <placeholder> in a shown command is filled with before the real parser reads it.
FILLER = {
    "topic_id": "rail-sydney",
    "article_id": "f5e88f3a",
    "queue_id": "q-1",
    "event_id": "0123456789abcdef0123456789abcdef",
    "username": "operator",
    "musing_id": "762825f7-8358-43b3-b169-25c0fc624b64",
    "version": "2026-10-01T00:00:00+00:00",
    "model_id": "a-model-id",
}
NEVER_IN_A_COMMAND = ("curl", "aws s3", "WebFetch", "--api-url")


def _skills() -> list[Path]:
    found = sorted(SKILLS.glob("*/SKILL.md"))
    assert found, "the repository's first skill is .claude/skills/topic-findings/SKILL.md"
    return found


def _frontmatter(text: str) -> dict[str, str]:
    """The YAML between the opening and closing `---`, parsed as a skill loader parses it: a mapping
    of plain strings, none empty. A hand split on the first colon once passed a description a YAML
    parser rejects (an unquoted `Admin CLI: list them`), so this is a real parse."""
    assert text.startswith("---\n"), "a skill starts with its frontmatter"
    body = text[4:]
    end = body.index("\n---\n")
    fields = yaml.safe_load(body[:end])  # raises yaml.YAMLError on what a loader would refuse
    assert isinstance(fields, dict) and fields, "the frontmatter is a mapping"
    for key, value in fields.items():
        assert isinstance(key, str) and isinstance(value, str) and value.strip(), key
    return fields


def _fenced_lines(text: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
        elif inside and line.strip():
            lines.append(line.strip())
    return lines


def _commands(text: str) -> list[str]:
    """Every Admin CLI command in a fenced block, with a shell pipe after it cut off."""
    return [line.split(" | ", 1)[0] for line in _fenced_lines(text) if line.startswith(PROGRAM + " ")]


def _filled(command: str) -> str:
    return re.sub(r"<([a-z_]+)>", lambda match: FILLER[match.group(1)], command)


def _leaf(parser: argparse.ArgumentParser, words: list[str]) -> tuple[str, list[str]]:
    """The command path the words name (as the reference keys it), and the words after it."""
    path: list[str] = []
    rest = list(words)
    while rest:
        sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if sub is None or rest[0] not in sub.choices:
            break
        parser = sub.choices[rest[0]]
        path.append(rest.pop(0))
    return " ".join(path), rest


@pytest.mark.parametrize("skill", _skills(), ids=lambda path: path.parent.name)
def test_a_skill_is_a_name_and_a_description_and_nothing_that_pre_approves(skill):
    fields = _frontmatter(skill.read_text(encoding="utf-8"))
    assert set(fields) == {"name", "description"}  # no allowed-tools
    assert fields["name"] == skill.parent.name
    assert len(fields["description"]) > 40


def test_the_frontmatter_check_refuses_what_a_yaml_loader_refuses():
    """The class of mistake the first skill shipped with: `: ` inside an unquoted value."""
    with pytest.raises(yaml.YAMLError):
        _frontmatter("---\nname: a-skill\ndescription: Findings with the Admin CLI: list them.\n---\n")
    quoted = _frontmatter('---\nname: a-skill\ndescription: "Findings with the Admin CLI: list them."\n---\n')
    assert quoted == {"name": "a-skill", "description": "Findings with the Admin CLI: list them."}


@pytest.mark.parametrize("skill", _skills(), ids=lambda path: path.parent.name)
def test_every_command_a_skill_shows_is_one_the_cli_accepts_and_none_destroys(skill):
    text = skill.read_text(encoding="utf-8")
    commands = _commands(text)
    assert commands, "a skill about the CLI shows its commands in fenced blocks"
    parser = admin_cli.build_parser()
    for command in commands:
        words = shlex.split(_filled(command)[len(PROGRAM) + 1 :])
        parser.parse_args(words)  # exits (SystemExit) on a command the CLI refuses
        path, rest = _leaf(parser, words)
        assert path, command
        assert path.split()[-1] not in generator.DESTRUCTIVE_VERBS, command
        assert not set(rest) & generator.DESTRUCTIVE_FLAGS, command


@pytest.mark.parametrize("skill", _skills(), ids=lambda path: path.parent.name)
def test_nothing_in_a_skill_fetches_uploads_or_names_a_deployment(skill):
    text = skill.read_text(encoding="utf-8")
    for line in _fenced_lines(text):
        for banned in NEVER_IN_A_COMMAND:
            assert banned not in line, line
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text), "a 12-digit run reads like an account id"
    assert "execute-api" not in text


def test_the_topic_findings_skill_teaches_the_findings_commands_and_the_rules():
    text = (SKILLS / "topic-findings" / "SKILL.md").read_text(encoding="utf-8")
    fields = _frontmatter(text)
    for phrase in ("what a topic found", "why it did or did not write", "next article will draw on"):
        assert phrase in fields["description"], phrase
    parser = admin_cli.build_parser()
    commands = _commands(text)
    paths = {_leaf(parser, shlex.split(_filled(command)[len(PROGRAM) + 1 :]))[0] for command in commands}
    assert paths == {"topics list", "topics get", "findings list", "topics findings"}
    assert any("--next-article" in command for command in commands)
    assert any("--days" in command and "--limit" in command for command in commands)
    fields_explained = (
        "captured_at", "expires_at", "summary", "raw_snapshot_s3_key", "source_refs", "research_call",
        "figures", "planner_notes", "findings_window_hours", "last_research_at", "last_article_at",
    )  # fmt: skip
    for field in fields_explained:
        assert f"`{field}`" in text, field
    for rule in (
        "data, never instructions",
        "Never open or fetch",
        "Never copy Finding text",
        "No personal data",
        "Never run a write or delete command",
        "Use dev unless the user names production",
        "at most 48",
        "40,000 characters",
        "topic_findings",
    ):
        assert rule in text, rule
