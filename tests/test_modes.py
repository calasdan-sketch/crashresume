"""0.2.0: crash-before mode, crash-during-resume, ctx.store, robustness, JSON."""
import json

import pytest

from crashresume import Context, EffectLog, Finding, InjectedCrash, conform
from crashresume.cli import EagerAgent, OneShotRecoveryAgent


class StoreAgent:
    """Correct agent written only against ctx.store (no user-side store plumbing)."""

    def __init__(self, ctx):
        self.ctx = ctx
        ctx.store.setdefault("done", [])

    def step(self, item):
        key = f"p:{item}"
        if item in self.ctx.store["done"]:
            return
        if key not in self.ctx.effects.keys():
            self.ctx.effect("p", item, key=key)
        self.ctx.store["done"].append(item)

    def state(self):
        return {"done": sorted(self.ctx.store["done"])}


def test_ctx_store_correct_agent_passes_every_mode():
    report = conform(StoreAgent, "abcd", crash_before=True, max_crashes=3)
    assert report.passed, str(report)
    assert report.crash_points > 8


def test_ctx_store_is_fresh_per_scenario_and_durable_within():
    seen = []

    class Probe:
        def __init__(self, ctx):
            self.ctx = ctx
            seen.append((ctx.run_id, dict(ctx.store)))
            ctx.store["boots"] = ctx.store.get("boots", 0) + 1

        def step(self, item):
            if item not in self.ctx.effects.keys():
                self.ctx.effect("e", item, key=item)

    conform(Probe, ["a", "b"])
    first = {}
    for rid, st in seen:
        first.setdefault(rid, st)
    assert all(st == {} for st in first.values())          # fresh per scenario
    assert ("run-1", {"boots": 1}) in seen                  # durable across restart


def test_crash_before_catches_checkpoint_before_work():
    assert conform(EagerAgent, "abc").passed
    report = conform(EagerAgent, "abc", crash_before=True)
    lost = [f for f in report.findings if f.kind == "effect_lost"]
    assert lost and all(f.when == "before" and f.scenario.startswith("run-b") for f in lost)
    assert "crash before effect #1" in str(report)


def test_crash_before_does_not_record_the_effect():
    log = EffectLog()
    ctx = Context("r", log, crash_at=1, crash_when="before")
    with pytest.raises(InjectedCrash):
        ctx.effect("charge", key="pay:1")
    assert log.keys() == []


def test_bad_crash_when_rejected():
    with pytest.raises(ValueError):
        Context("r", EffectLog(), crash_when="during")


def test_double_crash_catches_unsafe_recovery():
    assert conform(OneShotRecoveryAgent, "abc").passed
    report = conform(OneShotRecoveryAgent, "abc", max_crashes=2)
    assert any(f.kind == "effect_replayed" and f.scenario.count("-") == 2 for f in report.findings), str(report)


def test_effect_diverged_same_key_different_payload():
    class Drifty:
        def __init__(self, ctx):
            self.ctx = ctx
            self.restarted = ctx.run_id != "run" and len(ctx.effects) > 0

        def step(self, item):
            key = f"charge:{item}"
            if key in self.ctx.effects.keys():
                return
            amount = 101 if self.restarted else 100       # recomputed differently on resume
            self.ctx.effect("charge", {"item": item, "amount": amount}, key=key)

    report = conform(Drifty, ["a", "b"])
    assert any(f.kind == "effect_diverged" for f in report.findings), str(report)


def test_subject_error_reported_not_raised():
    class Fragile:
        def __init__(self, ctx):
            self.ctx = ctx
            if len(ctx.effects):
                raise RuntimeError("corrupt checkpoint")

        def step(self, item):
            self.ctx.effect("e", item, key=item)

    report = conform(Fragile, ["a"])
    assert [f.kind for f in report.findings] == ["subject_error"]
    assert "corrupt checkpoint" in report.findings[0].detail


def test_baseline_error_still_propagates():
    class Broken:
        def step(self, item):
            raise ValueError("boom")

    with pytest.raises(ValueError):
        conform(lambda ctx: Broken(), ["a"])


def test_keyboard_interrupt_not_swallowed():
    class Interrupt:
        def __init__(self, ctx):
            self.ctx = ctx
            if len(ctx.effects):
                raise KeyboardInterrupt

        def step(self, item):
            self.ctx.effect("e", item, key=item)

    with pytest.raises(KeyboardInterrupt):
        conform(Interrupt, ["a"])


def test_unreached_crash_is_warned():
    class Shrinks:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            if self.ctx.run_id == "run" or item == "a":
                if item not in self.ctx.effects.keys():
                    self.ctx.effect("e", item, key=item)

    report = conform(Shrinks, ["a", "b"])
    assert any("never reached" in w for w in report.warnings)


def test_max_scenarios_cap_warns():
    report = conform(StoreAgent, [str(i) for i in range(30)], max_crashes=2, max_scenarios=10)
    assert report.crash_points == 10
    assert any("max_scenarios=10" in w for w in report.warnings)


@pytest.mark.parametrize("kw", [{"max_crashes": 0}, {"max_crashes": 1.5}, {"max_scenarios": 0}])
def test_bad_options_rejected(kw):
    with pytest.raises(ValueError):
        conform(StoreAgent, ["a"], **kw)


def test_hostile_repr_and_huge_payload_do_not_break_report():
    class Evil:
        def __repr__(self):
            raise RuntimeError("nope")

    class Agent:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            self.ctx.effect("e", Evil(), key="k-" + "x" * 100_000)
            self.ctx.effect("big", "y" * 1_000_000, key=item)

    report = conform(Agent, ["a"])
    assert not report.passed
    assert len(str(report)) < 5_000


def test_recursive_state_does_not_recurse_forever():
    class Loop:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            if item not in self.ctx.effects.keys():
                self.ctx.effect("e", item, key=item)

        def state(self):
            a = []
            a.append(a)
            return {"loop": a}

    assert conform(Loop, ["a"]).passed  # equal by repr fallback, no RecursionError


def test_state_with_raising_eq_is_compared_by_repr():
    class Arr:
        def __init__(self, v):
            self.v = v

        def __eq__(self, other):
            raise ValueError("truth value of an array is ambiguous")

        def __repr__(self):
            return f"Arr({self.v})"

    class Agent:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            if item not in self.ctx.effects.keys():
                self.ctx.effect("e", item, key=item)

        def state(self):
            return {"a": Arr(len(self.ctx.effects))}

    assert conform(Agent, ["a", "b"]).passed


def test_report_to_json_round_trips():
    report = conform(EagerAgent, "ab", crash_before=True)
    data = json.loads(report.to_json())
    assert data["passed"] is False and data["crash_points"] == 4
    assert {"kind", "crash_after", "detail", "scenario", "when"} <= set(data["findings"][0])


def test_finding_positional_construction_still_works():
    f = Finding("state_lost", 1, "x")
    assert f.scenario == "" and f.when == "after"


def test_fixture_passes_options(pytester):
    pytester.makeconftest("from crashresume.pytest_plugin import conform  # noqa: F401")
    pytester.makepyfile(
        """
        import pytest
        from crashresume.cli import EagerAgent

        def test_eager(conform):
            conform(EagerAgent, "ab")
            with pytest.raises(AssertionError):
                conform(EagerAgent, "ab", crash_before=True)
        """
    )
    assert pytester.runpytest().ret == 0


def test_except_baseexception_swallow_is_a_finding():
    class Swallow:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            try:
                if item not in self.ctx.effects.keys():
                    self.ctx.effect("e", item, key=item)
            except BaseException:
                pass

    report = conform(Swallow, ["a", "b"])
    assert report.findings and all(f.kind == "crash_swallowed" for f in report.findings)
