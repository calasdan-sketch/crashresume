"""pytest integration: a ``conform`` fixture that asserts and returns a report."""
from __future__ import annotations

import pytest

from . import core


@pytest.fixture
def conform():
    """``conform(factory, inputs)`` - assert crash/restart conformance.

    Usage inside a test::

        def test_agent_is_conformant(conform, make_agent):
            conform(lambda ctx: make_agent(ctx), inputs=["a", "b", "c"])
            conform(make_agent, inputs, crash_before=True, max_crashes=2)
    """

    def run(factory, inputs, run_id: str = "run", allow_vacuous: bool = False, **options) -> core.ConformanceReport:
        report = core.conform(factory, inputs, run_id=run_id, **options)
        assert report.passed, str(report)
        assert allow_vacuous or not report.vacuous, (
            "no crash scenario ran (the subject recorded no effects through ctx.effect / ctx.tool / ctx.fs, "
            "or there were no inputs), so nothing was tested; pass allow_vacuous=True to accept that.\n" + str(report))
        return report

    return run
