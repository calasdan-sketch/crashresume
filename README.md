# crashresume

**Prove your agent survives a crash.** A conformance harness that kills and restarts a subject at every side-effect boundary, then checks the three invariants that decide whether a resume is safe:

| Invariant | Meaning |
|---|---|
| `state_lost` | after restart, persisted state is gone while the baseline had state |
| `effect_replayed` | the same idempotency key fired twice (e.g. a double charge) |
| `resume_diverged` | the resumed run finished in a different state than the baseline |

Real agent frameworks hit exactly these failures: streamed state vanishes on cancel, a payment succeeds but the client times out, the resumed run re-issues the payment. `crashresume` makes that testable without a framework — the subject is any object with a `step(input)` method.

## Install

```bash
pip install crashresume
```

## Usage

```python
from crashresume import conform

def make_agent(ctx):                 # called once per (re)start — model a fresh process
    return MyAgent(ctx, store=DB)    # your persistence lives in DB

report = conform(make_agent, inputs=["job-1", "job-2", "job-3"])
assert report.passed, str(report)
```

Your agent records side effects through the harness-owned context:

```python
class MyAgent:
    def __init__(self, ctx, store):
        self.ctx = ctx
        self.store = store
        self.done = set(store.load("done"))

    def step(self, job):
        if job in self.done:
            return                       # resume-safe: skip what already committed
        self.ctx.effect("charge", job, key=f"charge:{job}")
        self.done.add(job)
        self.store.save("done", self.done)
```

The harness runs a baseline, then re-runs crashing right after each recorded effect (side effect committed, process dead before checkpoint — the nastiest real-world case), restarts via your factory, and compares state and effect logs.

### Per-scenario state (important)

Each crash point is an **independent scenario** — your factory is called from scratch each time. Durable state that must survive a restart but must not leak between scenarios should be keyed by `ctx.run_id` (the baseline uses `"run"`; crash point *k* and its resume share `"run-<k>"`):

```python
stores = {}

def make_agent(ctx):
    store = stores.setdefault(ctx.run_id, Store())   # fresh per scenario
    return MyAgent(ctx, store)                        # durable within the scenario
```

If your durable store is a real database, reset its fixture per scenario the same way.

## pytest plugin

A `conform` fixture is registered automatically:

```python
def test_agent_is_crash_safe(conform):
    conform(lambda ctx: MyAgent(ctx, store=DB), inputs=["a", "b", "c"])
```

## CLI demo

```bash
crashresume --selftest   # runs a conforming agent plus two broken ones and verifies the findings
```

## Notes

- Subjects that record no effects through `ctx` pass trivially — route real side effects through `ctx.effect` or the harness cannot observe them.
- `state_lost` vs `resume_diverged`: an empty state after restart is "lost"; a non-empty but different final state is "diverged". Expose a `state()` method to opt into state comparison.
- Works with any persistence: SQLite, Redis, files, LangGraph checkpoints.

## License

MIT-style: use it, ship it, no warranty.
