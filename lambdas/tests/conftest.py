"""Shared test guards."""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "real_probe: run common.model_catalog.probe_access itself")
    config.addinivalue_line("markers", "call_traces: read trace_calls from the evals settings itself")


@pytest.fixture(autouse=True)
def _no_access_probes(request, monkeypatch):
    """Never let a test make a real Bedrock call through the model catalog's access probe
    (model_catalog.probe_access, a 1-token Converse call) with whatever credentials the machine
    has: it answers "unknown" unless the test is marked real_probe."""
    if request.node.get_closest_marker("real_probe"):
        return
    from common import model_catalog

    def not_probed(model_id, client=None):
        return "unknown", "not probed"

    monkeypatch.setattr(model_catalog, "probe_access", not_probed)


@pytest.fixture(autouse=True)
def _call_traces_off(request, monkeypatch):
    """Never let a test ask the evals row whether to trace its model calls (common/call_traces.py):
    every daily cycle, rewrite and research tick binds a run, whose first call would otherwise read
    DynamoDB wherever the test points MODEL_CONFIG_TABLE. Off, unless the test is marked
    call_traces; tests of the traces themselves turn it on."""
    if request.node.get_closest_marker("call_traces"):
        return
    from common import call_traces

    monkeypatch.setattr(call_traces, "_trace_calls_on", lambda: False)
