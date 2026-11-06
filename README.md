# crashresume

**Prove your agent survives a crash - in a unit test, with zero dependencies.** `crashresume` kills and restarts your agent at every side-effect boundary (after the effect commits, before it commits, and again during recovery), then tells you exactly which crash point double-charged, lost work, or resumed into the wrong state.

No server, no database, no framework: the subject is any object with a `step(input)` method. Pure Python 3.9+, no runtime dependencies (pytest only if you use the fixture).

## Quick start

```bash
pip install crashresume
crashresume --selftest      # one correct agent passes, four broken ones are caught
```

```python
from crashresume import conform

class Agent:
    def __init__(self, ctx):                     # called on every (re)start = a fresh process
        self.ctx = ctx
        ctx.store.setdefault("done", [])         # ctx.store: durable across restarts, fresh per scenario

    def step(self, job):
        key = f"charge:{job}"
        if job in self.ctx.store["done"]:
            return                               # already checkpointed
        if key not in self.ctx.effects.keys():   # reconcile against effects that already happened
            self.ctx.effect("charge", job, key=key)
        self.ctx.store["done"].append(job)

    def state(self):                             # optional: opt into state comparison
        return sorted(self.ctx.store["done"])

report = conform(Agent, ["job-1", "job-2", "job-3"], crash_before=True, max_crashes=2)
assert report.passed, str(report)
```

When it fails you get the scenario and the reason (output from the built-in `EagerAgent` demo):

```
conformance: 6 crash point(s) tested, 3 finding(s)
  [effect_lost] crash before effect #1 (run-b1): key 'process:a' never issued after resume
  [effect_lost] crash before effect #2 (run-b2): key 'process:b' never issued after resume
  [effect_lost] crash before effect #3 (run-b3): key 'process:c' never issued after resume
```

## What it checks

| Finding | Meaning |
|---|---|
| `effect_replayed` | an idempotency key fired more often than in the baseline (e.g. a double charge) |
| `effect_lost` | a keyed effect from the baseline happened fewer times after resume (usually never) |
| `effect_diverged` | opt-in (`check_payloads=True` or `payload_key=`): after a crash, a key was issued with a different payload than in the baseline |
| `state_lost` | final state is empty while the baseline had state |
| `resume_diverged` | final state is non-empty but differs from the baseline |
| `resume_crashed` | the clean resume hit an injected crash again |
| `subject_error` | the subject raised an ordinary exception after a crash (reported, not raised) |
| `crash_swallowed` | the subject caught the injected crash (`except BaseException`) and kept going, even if it died again later |

Warnings (not failures) flag runs with no effects, a baseline that itself issues a key more than once, keyless effects that repeated, injected crashes that were never reached, and a hit `max_scenarios` cap - a truncated or untested run is never reported as a silent pass.

## Crash modes

| Option | Crash point | Catches |
|---|---|---|
| default | right **after** each effect commits, before your checkpoint | replays (the classic double charge) |
| `crash_before=True` | right **before** each effect commits | checkpointing "done" before doing the work |
| `max_crashes=2` (or more) | also crash the **resume** at each of its own effects | recovery code that is only safe once |
| `max_scenarios=10_000` | safety cap on scenarios run (breadth-first) | keeps `max_crashes>=3` bounded |

Scenarios are named by `ctx.run_id`: baseline `run`, `run-3` (after effect 3), `run-b3` (before effect 3), `run-3-1` (after 3, then after the resume's first effect).

## pytest

A `conform` fixture is registered automatically and asserts for you:

```python
def test_agent_is_crash_safe(conform):
    conform(Agent, ["a", "b", "c"], crash_before=True, max_crashes=2)
```

CI artifacts: `report.to_json(indent=2)` (or `to_dict()`).

Payload checks: `effect_diverged` is off by default because request ids and timestamps legitimately differ between runs. Turn it on with `check_payloads=True`, or compare only what matters with `payload_key=lambda p: (p["account"], p["amount"])`. Payloads are compared with `==` first (dict key order does not matter); repr is used only for types without their own equality, with memory addresses removed.

## Your own persistence

`ctx.store` is the zero-setup option. Each new process life gets a `copy.deepcopy` of it, so plain data survives a crash but live objects (the old agent, open connections) do not; objects that cannot be copied (locks, generators, sockets) raise a clear `TypeError`. `isolate_store=False` shares one live dict instead. If your agent uses a real database, reset it per scenario by keying on `ctx.run_id`:

```python
stores = {}

def make_agent(ctx):
    return MyAgent(ctx, store=stores.setdefault(ctx.run_id, Store()))
```

Route real side effects through `ctx.effect(kind, payload, key=...)` (keys must be hashable; strings recommended, and `""` counts as a real key) - that is where crashes are injected and replays counted. `ctx.effects` is the durable log of what already happened, for reconciliation.

## How it compares

Durable-execution runtimes test *their own* recovery; `crashresume` tests *your* agent's. Statements below come from each project's own pages.

| | crashresume | [durabl](https://pypi.org/project/durabl/) | [Temporal replay testing](https://keithtenzer.com/temporal/Altering_Space-Time_Continuum_Testing_for_Determinism/) | [ordeal](https://pypi.org/project/ordeal/0.1.5/) | [agent-effects (Rust)](https://docs.rs/crate/agent-effects/latest/source/tests/crash.rs) |
|---|---|---|---|---|---|
| What it is | test harness for any `step()` object | durable runtime (SQLite log) with a chaos suite | determinism check of workflow code against recorded histories | chaos testing / fault injection library | effect runtime with per-crash-point tests |
| Tests your code without adopting a runtime | yes | no | no (Temporal workflows) | yes | no |
| Crash at every effect boundary, automatically | yes | yes, 10 instrumented points in its runtime | no (replays given histories) | intermittent crash on a schedule | yes, per lifecycle point |
| Crash during recovery | yes (`max_crashes`) | yes (`DURING_REPLAY`) | no | not described | not described |
| Named idempotency-violation findings | yes | its own runtime's invariants | non-determinism errors | user-written properties | at-most-once assertions |
| Python, zero runtime deps | yes | Python 3.11+ | SDK required | Python | Rust |

Why it matters: crashes between a side effect and its record repeat the effect - one write-up reproduced a duplicate charge in 3 of 3 trials on Restate ([source](https://nerdleveltech.com/restate-durable-agent-duplicate-tool-calls-tested)), and LangGraph re-runs an interrupted node from the top, so tools that already ran fire again ([source](https://blog.raed.dev/posts/langgraph-hitl)). `crashresume` lets you catch that class of bug in a unit test.

## Honest limits

- **Simulated crashes, same process.** `InjectedCrash` (a `BaseException`) models process death; it does not `SIGKILL` a real process, drop network connections, or lose un-fsynced writes. `ctx.store` writes are durable immediately (no partial-write modelling), and anything `copy.deepcopy` can copy (including list iterators) survives.
- A subject that converts the injected crash into another exception (`except BaseException: raise X`) is reported as `subject_error`, not `crash_swallowed`.
- **Only what goes through `ctx.effect` is observed.** Subjects that record no effects pass trivially (with a warning). Effects without `key=` cannot be checked for replay (warning only).
- **Deterministic subjects work best.** Crash points come from the baseline run; a subject that issues different effects each run gets "never reached" warnings.
- **Cost grows with depth.** For N effects and an agent that resumes where it left off, the default runs N scenarios; `max_crashes=2` runs N + N(N+1)/2 (65 for N=10); `crash_before=True` roughly quadruples that (240 for N=10); `max_crashes=3` gives 285, or 2000 with `crash_before`. Each scenario replays all inputs. `max_scenarios` (default 10,000) caps both runtime and the pending-scenario queue.
- Synchronous `step()` only; async agents need a sync wrapper for now.

## License

MIT. See `LICENSE`.
