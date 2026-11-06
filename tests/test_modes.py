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

    assert not any(f.kind == "effect_diverged" for f in conform(Drifty, ["a", "b"]).findings)  # opt-in
    report = conform(Drifty, ["a", "b"], check_payloads=True)
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


# ---- panel review regressions (0.2.0 review) ----------------------------------------

def _keyed_agent(make_payload):
    class A:
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("done", [])

        def step(self, j):
            if j in self.ctx.store["done"]:
                return
            k = f"c:{j}"
            if k not in self.ctx.effects.keys():
                self.ctx.effect("charge", make_payload(self.ctx, j), key=k)
            self.ctx.store["done"].append(j)

        def state(self):
            return sorted(self.ctx.store["done"])
    return A


class _P:
    def __init__(self, v):
        self.v = v

    def __eq__(self, o):
        return isinstance(o, _P) and o.v == self.v


class _Plain:          # no __eq__: default repr carries a memory address
    def __init__(self, v):
        self.v = v


def test_r1_uuid_payload_passes_by_default_and_payload_key_scopes_check():
    import uuid
    Agent = _keyed_agent(lambda ctx, j: {"job": j, "req": str(uuid.uuid4())})
    assert conform(Agent, "abc").passed
    assert conform(Agent, "abc", payload_key=lambda p: p["job"]).passed
    assert not conform(Agent, "abc", check_payloads=True).passed


@pytest.mark.parametrize("make", [
    lambda ctx, j: {"a": 1, "b": 2} if ctx.run_id == "run" else {"b": 2, "a": 1},
    lambda ctx, j: _P(j),
    lambda ctx, j: _Plain(j),
])
def test_r1_equal_payloads_not_flagged_even_when_checking(make):
    report = conform(_keyed_agent(make), "abc", check_payloads=True, crash_before=True)
    assert report.passed, str(report)


def test_r1_first_life_effects_are_not_payload_checked():
    # payload differs in EVERY non-baseline life, but only the crashed life issues it
    class Once:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            if f"c:{j}" not in self.ctx.effects.keys():
                self.ctx.effect("charge", (j, self.ctx.run_id), key=f"c:{j}")

    report = conform(Once, ["a"], check_payloads=True)
    assert report.passed, str(report)   # effect issued only before the crash


def test_r2_queue_memory_bounded_by_cap():
    import tracemalloc

    class Many:
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("n", 0)

        def step(self, j):
            if self.ctx.store["n"] > j:
                return
            self.ctx.effect("e", j, key=str(j))
            self.ctx.store["n"] = j + 1

    tracemalloc.start()
    report = conform(Many, range(150), max_crashes=2, max_scenarios=150)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert report.crash_points == 150
    assert peak < 3_000_000, peak                      # unbounded queue was ~11k entries here
    assert any("at least 11325 more" in w for w in report.warnings), report.warnings


def test_r3_partial_swallow_then_later_death_is_flagged():
    class Swallow1:
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("done", [])

        def step(self, j):
            if j in self.ctx.store["done"]:
                return
            if f"c:{j}" not in self.ctx.effects.keys():
                if j == "a":
                    try:
                        self.ctx.effect("charge", j, key=f"c:{j}")
                    except BaseException:
                        pass
                else:
                    self.ctx.effect("charge", j, key=f"c:{j}")
            self.ctx.store["done"].append(j)

        def state(self):
            return sorted(self.ctx.store["done"])

    report = conform(Swallow1, "abc")
    assert any(f.kind == "crash_swallowed" and f.scenario == "run-1" for f in report.findings), str(report)


def test_r4_store_is_copied_between_lives():
    seen = []

    class Alias:
        def __init__(self, ctx):
            self.ctx = ctx
            seen.append(ctx.store)
            ctx.store.setdefault("box", []).append(ctx.run_id)

        def step(self, j):
            if j not in self.ctx.effects.keys():
                self.ctx.effect("e", j, key=j)

    conform(Alias, ["a"])
    crash_life, resume_life = [st for st in seen if "run-1" in st["box"]][:2]
    assert crash_life is not resume_life and crash_life["box"] is not resume_life["box"]
    assert resume_life["box"] == ["run-1", "run-1"]     # contents still durable
    seen.clear()
    conform(Alias, ["a"], isolate_store=False)
    assert seen[1] is seen[2]                           # opt-out shares the live dict


def test_r4_unsurvivable_store_object_raises_clear_error():
    import threading

    class Locky:
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("lock", threading.Lock())

        def step(self, j):
            self.ctx.effect("e", j, key=j)

    with pytest.raises(TypeError, match="isolate_store=False"):
        conform(Locky, ["a"])


def test_r5_baseline_repeated_key_warns():
    class DoubleAlways:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            while self.ctx.effects.keys().count(f"c:{j}") < 2:
                self.ctx.effect("charge", j, key=f"c:{j}")

    report = conform(DoubleAlways, "ab", crash_before=True, max_crashes=2)
    assert any("baseline issued key 'c:a' 2 times" in w for w in report.warnings)


def test_r5_key_count_drop_is_effect_lost():
    class Twice:
        def __init__(self, ctx):
            self.ctx = ctx
            self.ctx.store.setdefault("n", 0)

        def step(self, j):
            if self.ctx.store["n"] > j:
                return
            self.ctx.store["n"] = j + 1 if self.ctx.run_id == "run" or j == 0 else 99
            self.ctx.effect("e", j, key="same")

    report = conform(Twice, range(2), crash_before=True)
    assert any(f.kind == "effect_lost" and "issued 1 time(s), baseline 2" in f.detail for f in report.findings), str(report)


def test_r5_module_docstring_matches_behaviour():
    import crashresume.core as core
    assert "issued twice, or" not in core.__doc__


def test_r6_unhashable_key_rejected_clearly():
    class UK:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            self.ctx.effect("e", j, key=[j])

    with pytest.raises(TypeError, match="must be hashable"):
        conform(UK, "ab")


def test_r6_empty_string_key_is_a_real_key():
    class Empty:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            self.ctx.effect("e", j, key="")

    report = conform(Empty, ["a"])
    assert any(f.kind == "effect_replayed" for f in report.findings)


def test_r7_readme_numbers_match_reality():
    import pathlib
    import re
    readme = (pathlib.Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    block = re.search(r"```\n(conformance: .*?)```", readme, re.S).group(1)
    n = int(re.search(r"(\d+) finding", block).group(1))
    assert n == len([ln for ln in block.splitlines() if ln.strip().startswith("[")])
    assert "five kinds" not in readme

    class S:
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("n", 0)

        def step(self, j):
            if self.ctx.store["n"] > j:
                return
            self.ctx.effect("e", j, key=str(j))
            self.ctx.store["n"] = j + 1

    assert conform(S, range(10), max_crashes=2).crash_points == 65
    assert conform(S, range(10), max_crashes=2, crash_before=True).crash_points == 240
    assert "(65 for N=10)" in readme and "240" in readme
