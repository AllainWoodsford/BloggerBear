"""Shared test guards."""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "real_probe: run common.model_catalog.probe_access itself")


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
