"""No print in the pipeline's handlers or adapters interpolates an exception without `!r`.

The evidence trail (common/evidence.py) is read from the same log groups these functions print to,
and the Python runtime writes each newline-terminated line of stdout as its own CloudWatch event.
A print of `str(exc)` where the exception's message is third-party text (a news provider's error,
a scene's URL in a worker error) could carry a newline followed by a well-formed evidence line,
and forge one. `{exc!r}` escapes newlines. Readers parse every line anyway (rule 7), and this test
keeps the prints honest: every f-string handed to `print` that interpolates a name bound by an
`except ... as` clause uses the `!r` conversion.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

LAMBDAS = Path(__file__).resolve().parents[1]
GUARDED = sorted(
    [
        "research_tick_handler.py",
        "daily_cycle_handler.py",
        "admin_api_handler.py",
        "vision_worker_handler.py",
        "eval_jobs_handler.py",
        "dispatcher_handler.py",
        # Imported by every pipeline Lambda, and prints when Logs refuses an event.
        "common/evals.py",
        # eval-jobs' rollup and the admin API's evals routes, which print what they could not read.
        "common/evals_rollup.py",
        "common/evals_settings.py",
        "common/evals_status.py",
        *(str(path.relative_to(LAMBDAS)) for path in (LAMBDAS / "common" / "adapters").glob("*.py")),
    ]
)


def _except_names(tree: ast.AST) -> set[str]:
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler) and node.name}


def _printed_f_strings(tree: ast.AST):
    """Every f-string (JoinedStr) anywhere inside a `print(...)` call's arguments."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                for inner in ast.walk(arg):
                    if isinstance(inner, ast.JoinedStr):
                        yield node.lineno, inner


def raw_exception_prints(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = _except_names(tree)
    found = []
    for lineno, joined in _printed_f_strings(tree):
        for value in joined.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            expr = value.value
            if isinstance(expr, ast.Name) and expr.id in names and value.conversion != ord("r"):
                found.append(f"{path.name}:{lineno} prints {{{expr.id}}} without !r")
    return found


@pytest.mark.parametrize("name", GUARDED)
def test_no_print_interpolates_an_exception_without_repr(name):
    assert raw_exception_prints(LAMBDAS / name) == []


def test_the_guard_sees_a_raw_print_and_accepts_the_repr(tmp_path):
    source = (
        "try:\n    pass\nexcept Exception as exc:\n"
        "    print(f\"failed: {exc}\")\n    print(f\"failed: {exc!r}\")\n    print(\"x\" + f\"({exc})\")\n"
        "    detail = f\"{exc}\"\n"
    )
    path = tmp_path / "sample.py"
    path.write_text(source)
    assert raw_exception_prints(path) == [
        "sample.py:4 prints {exc} without !r", "sample.py:6 prints {exc} without !r",
    ]  # fmt: skip


def test_the_three_prints_the_trail_found_now_use_repr():
    """research_tick_handler's planner-notes print and crypto_feed's two headline prints
    interpolated a news provider's or an adapter's exception as text."""
    tick = (LAMBDAS / "research_tick_handler.py").read_text(encoding="utf-8")
    crypto = (LAMBDAS / "common" / "adapters" / "crypto_feed.py").read_text(encoding="utf-8")
    assert "planner notes dropped: {type(exc).__name__}: {exc!r}" in tick
    assert "no headlines this tick ({exc!r})" in crypto and "no headlines for the review ({exc!r})" in crypto
