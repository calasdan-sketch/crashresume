"""pytest integration: a ``conform`` fixture that asserts and returns a report."""
from __future__ import annotations

import pytest

from . import core


@pytest.fixture
def conform():
    """``conform(factory, inputs)`` — assert crash/restart conformance.

    Usage inside a test::

        def test_agent_is_conformant(conform, make_agent):
            conform(lambda ctx: make_agent(ctx), inputs=["a", "b", "c"])
    """

    def run(factory, inputs, run_id: str = "run") -> core.ConformanceReport:
        report = core.conform(factory, inputs, run_id=run_id)
        assert report.passed, str(report)
        return report

    return run
