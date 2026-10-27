"""The shared Logs Insights runner (common/logs_insights.py), moved out of ops_mcp/logs.py.

Its behaviour is held by test_ops_mcp_logs.py, which still calls it through ops_mcp.logs. What is
held here is why it moved (docs/enhancements/model-ranch-prompt-assets-evals.md, E0): pipeline code
(the evals rollup first) is to run its queries with it, so it must need nothing from the assistant,
and a package that copies common/ without ops_mcp/logs.py (as a module's own package can: the ops
agent's takes all of common/ and four other ops_mcp files) can still import it; and the assistant
must be using this one rather than a copy.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from common import logs_insights
from ops_mcp import logs

LAMBDAS = Path(__file__).resolve().parents[1]


def test_the_runner_imports_nothing_from_the_assistant():
    """Only the standard library and the AWS SDK, read from the file's own imports."""
    tree = ast.parse((LAMBDAS / "common" / "logs_insights.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"boto3", "botocore"}
    assert imported <= allowed, imported - allowed


def test_importing_the_runner_loads_no_assistant_module():
    """The same, as Python sees it: a fresh interpreter that imports the runner has loaded no
    ops_mcp or ops_agent module, so a Lambda package without them can import it."""
    probe = (
        "import sys\n"
        "import common.logs_insights\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('ops_mcp', 'ops_agent')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=LAMBDAS, capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == "[]"


def test_the_assistant_runs_its_queries_with_the_runner_in_common():
    """ops_mcp.logs hands out the runner, its shared client and its limits by their old names, and
    they are the very objects in common/, not a copy that could drift from them."""
    assert logs.run_queries is logs_insights.run_queries
    assert logs.client is logs_insights.client
    for name in ("QUERY_LIMIT", "WAIT_SECONDS", "POLL_SECONDS", "THROTTLES"):
        assert getattr(logs, name) is getattr(logs_insights, name), name
    source = (LAMBDAS / "ops_mcp" / "logs.py").read_text(encoding="utf-8")
    assert "def run_queries(" not in source and "boto3.client(" not in source


# --- the row limit (docs/enhancements/model-ranch-prompt-assets-evals.md, section 7.1, PR13) ------


class RecordingLogs:
    """A Logs client that answers every query at once with one row, and keeps what each StartQuery
    asked for."""

    def __init__(self):
        self.asked: list[dict] = []

    def start_query(self, **kwargs):
        self.asked.append(kwargs)
        return {"queryId": f"q{len(self.asked)}"}

    def get_query_results(self, queryId):  # noqa: N803
        return {"status": "Complete", "results": [[{"field": "n", "value": "1"}]]}

    def stop_query(self, queryId):  # noqa: N803
        raise AssertionError("nothing is left running")


NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
JOB = (("evals", "q"), "ap-southeast-2", "the-group", "fields @message", NOW, NOW)


def test_a_query_asks_for_the_assistants_thousand_rows_when_no_limit_is_given():
    fake = RecordingLogs()

    results = logs_insights.run_queries([JOB], client=lambda region: fake, sleep=lambda s: None)

    assert results == {("evals", "q"): [{"n": "1"}]}
    assert [asked["limit"] for asked in fake.asked] == [logs_insights.QUERY_LIMIT] == [1000]


def test_a_caller_may_ask_for_up_to_ten_thousand_rows_a_query():
    fake = RecordingLogs()

    logs_insights.run_queries([JOB, (("b", "q"), *JOB[1:])], client=lambda region: fake, limit=10_000)

    assert [asked["limit"] for asked in fake.asked] == [10_000, 10_000]
    assert logs_insights.MAX_QUERY_LIMIT == 10_000


def test_a_throttled_start_tried_again_asks_for_the_same_limit():
    class ThrottledOnce(RecordingLogs):
        def start_query(self, **kwargs):
            if not self.asked:
                self.asked.append(kwargs)
                raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "StartQuery")
            return super().start_query(**kwargs)

    fake = ThrottledOnce()

    results = logs_insights.run_queries(
        [JOB], client=lambda region: fake, sleep=lambda s: None, recover_throttles=True, limit=5000
    )

    assert results == {("evals", "q"): [{"n": "1"}]}
    assert [asked["limit"] for asked in fake.asked] == [5000, 5000]


def test_the_limit_is_keyword_only_and_within_logs_insights_ceiling():
    """Positional, it would land in whatever parameter came first; and Logs Insights refuses more
    than 10,000 rows, so asking for more is a mistake to say at once rather than a query that fails."""
    parameter = inspect.signature(logs_insights.run_queries).parameters["limit"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default == 1000
    for wrong in (0, 10_001, 2.5, True, "100"):
        with pytest.raises(ValueError):
            logs_insights.run_queries([JOB], client=lambda region: RecordingLogs(), limit=wrong)
