"""Core conformance-engine tests.

Every factory here keys its durable store by ``ctx.run_id`` so that each crash
scenario starts from the same initial state as the baseline — the documented
isolation protocol.
"""
from crashresume import ConformanceReport, Context, EffectLog, InjectedCrash, conform


class Store:
    def __init__(self):
        self.data = {}


class Stores:
    """Per-scenario stores, isolated by run_id."""

    def __init__(self):
        self.d = {}

    def get(self, ctx):
        return self.d.setdefault(ctx.run_id, Store())


class GoodAgent:
    def __init__(self, ctx: Context, store: Store):
        self.ctx, self.store = ctx, store
        self.done = set(store.data.get("done", set()))

    def step(self, item):
        key = f"process:{item}"
        if item in self.done or key in self.ctx.effects.keys():
            self.done.add(item)
            self.store.data["done"] = set(self.done)
            return
        self.ctx.effect("process", item, key=key)
        self.done.add(item)
        self.store.data["done"] = set(self.done)

    def state(self):
        return {"done": sorted(self.store.data.get("done", set()))}


class LazyAgent:
    def __init__(self, ctx: Context, store: Store):
        self.ctx = ctx
        self.done = []

    def step(self, item):
        self.ctx.effect("process", item, key=f"process:{item}")
        self.done.append(item)

    def state(self):
        return {"done": list(self.done)}


class ReplayAgent:
    def __init__(self, ctx: Context, store: Store):
        self.ctx, self.store = ctx, store

    def step(self, item):
        self.ctx.effect("process", item, key=f"process:{item}")
        self.store.data.setdefault("done", set()).add(item)

    def state(self):
        return {"done": sorted(self.store.data.get("done", set()))}


class UnkeyedAgent:
    """No idempotency keys; harness falls back to state comparison only."""

    def __init__(self, ctx: Context, store: Store):
        self.ctx, self.store = ctx, store
        self.n = store.data.get("n", 0)

    def step(self, item):
        if self.n >= 3:
            return
        self.ctx.effect("process", item)
        self.n += 1
        self.store.data["n"] = self.n

    def state(self):
        return {"n": self.n}


def test_good_agent_passes():
    stores = Stores()
    report = conform(lambda ctx: GoodAgent(ctx, stores.get(ctx)), ["a", "b", "c"])
    assert isinstance(report, ConformanceReport)
    assert report.passed, str(report)
    assert report.crash_points == 3


def test_lazy_agent_flagged_as_replay():
    stores = Stores()
    report = conform(lambda ctx: LazyAgent(ctx, stores.get(ctx)), ["a", "b"])
    assert not report.passed
    assert any(f.kind == "effect_replayed" for f in report.findings)


def test_replay_agent_flagged():
    stores = Stores()
    report = conform(lambda ctx: ReplayAgent(ctx, stores.get(ctx)), ["a", "b"])
    assert not report.passed
    assert any(f.kind == "effect_replayed" for f in report.findings)


def test_unkeyed_agent_still_conforms_when_count_stable():
    stores = Stores()
    report = conform(lambda ctx: UnkeyedAgent(ctx, stores.get(ctx)), ["a", "b", "c", "d"])
    assert report.passed, str(report)


def test_no_effects_trivially_passes():
    class Silent:
        def step(self, item):
            pass

    report = conform(lambda ctx: Silent(), ["x"])
    assert report.passed and report.crash_points == 0


def test_state_lost_when_subject_stops_reading_after_lost_checkpoint():
    # classic offset-commit failure: checkpoint lost, subject wrongly believes
    # the stream is exhausted -> final state empty though baseline had state
    stores = Stores()

    class Agent:
        def __init__(self, ctx, store):
            self.ctx, self.store = ctx, store

        def step(self, item):
            if self.store.data.get("exhausted"):
                return
            self.ctx.effect("e", item, key=item)
            self.store.data.setdefault("done", []).append(item)

        def state(self):
            return {"done": list(self.store.data.get("done", []))}

    def factory(ctx):
        store = stores.get(ctx)
        if ctx.run_id != "run":  # simulate: checkpoint lost in the crash
            store.data["done"] = []
            store.data["exhausted"] = True
        return Agent(ctx, store)

    report = conform(factory, ["a", "b"])
    assert any(f.kind == "state_lost" for f in report.findings), str(report)


def test_resume_diverged_when_final_state_differs():
    stores = Stores()

    class Agent:
        def __init__(self, ctx, store):
            self.ctx, self.store = ctx, store

        def step(self, item):
            key = f"e:{item}"
            if key in self.ctx.effects.keys():
                return
            self.ctx.effect("e", item, key=key)
            self.store.data.setdefault("done", []).append(item)

        def state(self):
            return {"done": sorted(self.store.data.get("done", []))}

    def factory(ctx):
        store = stores.get(ctx)
        if ctx.run_id != "run":
            store.data["done"] = ["ghost"]  # corrupted but non-empty checkpoint
        return Agent(ctx, store)

    report = conform(factory, ["a", "b"])
    assert any(f.kind == "resume_diverged" for f in report.findings), str(report)


def test_crash_injected_at_every_boundary():
    stores = Stores()
    seen = []

    class Recorder:
        def __init__(self, ctx, store):
            self.ctx, self.store = ctx, store
            self.done = set(store.data.get("done", set()))

        def step(self, item):
            if item in self.done or item in self.ctx.effects.keys():
                self.done.add(item)
                self.store.data["done"] = set(self.done)
                return
            seen.append((self.ctx.run_id, tuple(self.ctx.effects.keys())))
            self.ctx.effect("e", item, key=item)
            self.done.add(item)
            self.store.data["done"] = set(self.done)

    conform(lambda ctx: Recorder(ctx, stores.get(ctx)), ["1", "2"])
    run_ids = [r[0] for r in seen]
    assert len(set(run_ids)) >= 3


def test_report_str_lists_findings():
    stores = Stores()
    report = conform(lambda ctx: ReplayAgent(ctx, stores.get(ctx)), ["a"])
    text = str(report)
    assert "effect_replayed" in text and "crash after effect #1" in text


def test_effect_log_shared_across_restart():
    log = EffectLog()
    ctx = Context("r", log)
    ctx.effect("a", key="k1")
    assert log.keys() == ["k1"]


def test_injected_crash_raised_after_recording():
    log = EffectLog()
    ctx = Context("r", log, crash_at=1)
    try:
        ctx.effect("charge", key="pay:1")
        raised = False
    except InjectedCrash:
        raised = True
    assert raised and log.keys() == ["pay:1"]


def test_scenarios_are_isolated():
    # the baseline must not poison crash scenarios via shared durable state
    stores = Stores()
    starts = []

    class Agent:
        def __init__(self, ctx, store):
            self.ctx, self.store = ctx, store
            self.done = set(store.data.get("done", set()))
            starts.append((ctx.run_id, sorted(self.done)))

        def step(self, item):
            key = f"e:{item}"
            if key in self.ctx.effects.keys():
                self.store.data.setdefault("done", set()).add(item)  # heal from log
                return
            self.ctx.effect("e", item, key=key)
            self.store.data.setdefault("done", set()).add(item)

        def state(self):
            return {"done": sorted(self.store.data.get("done", set()))}

    report = conform(lambda ctx: Agent(ctx, stores.get(ctx)), ["a", "b"])
    assert report.passed, str(report)
    # the FIRST construction of every scenario starts from an empty checkpoint
    # (resumes within a scenario may see that scenario's crash-run state)
    first_seen = {}
    for rid, done in starts:
        first_seen.setdefault(rid, done)
    assert all(done == [] for done in first_seen.values())
