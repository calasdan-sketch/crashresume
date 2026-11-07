# crashresume

**Prove your agent survives a crash - in a unit test, with zero dependencies.** `crashresume` kills and restarts your agent at every side-effect and file-I/O boundary (after the effect commits, before it commits, mid-write with a torn file, and again during recovery - in simulation or with a real `SIGKILL`), then tells you exactly which crash point double-charged, lost work, or resumed into the wrong state, and shrinks it to a minimal reproduction.

No server, no database, no framework: the subject is any object with a `step(input)` method. Pure Python 3.9+, no runtime dependencies (pytest only if you use the fixture).

On a labelled corpus of 24 sample agents (16 intentionally broken) the thorough mode finds **16/16 bugs with 0 false alarms**; killing at 5 random points, chaos-monkey style, finds about half (see [Benchmark](#benchmark)).

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
| `subject_hang` | process mode only: a child exceeded its timeout and was killed |

Warnings (not failures) flag runs with no effects, a baseline that itself issues a key more than once, keyless effects that repeated, injected crashes that were never reached, and a hit `max_scenarios` cap - a truncated or untested run is never reported as a silent pass.

## Crash modes

| Option | Crash point | Catches |
|---|---|---|
| default | right **after** each effect commits, before your checkpoint | replays (the classic double charge) |
| `crash_before=True` | right **before** each effect commits | checkpointing "done" before doing the work |
| `max_crashes=2` (or more) | also crash the **resume** at each of its own effects | recovery code that is only safe once |
| `torn_writes=True` | after each `ctx.fs.write`, power cut with the un-fsynced tail torn to half | non-atomic checkpoint files, unvalidated journal tails |
| `strict_keyless=True` | repeated or vanished keyless effects become findings | duplicate emails with no idempotency key |
| `thorough=True` | all of the above plus `max_crashes>=2` | the whole menu in one switch |
| `max_scenarios=10_000` | safety cap on scenarios run (breadth-first) | keeps `max_crashes>=3` bounded |

Scenarios are named by `ctx.run_id`: baseline `run`, `run-3` (after crash point 3), `run-b3` (before point 3), `run-t3` (torn write at point 3), `run-3-1` (after 3, then after the resume's first point). A crash point is every `ctx.effect` / `ctx.tool` call and every mutating `ctx.fs` call.

## Files: fsync, rename, torn writes

`ctx.fs` is an in-memory file system that survives restarts *minus whatever was never fsynced*. Data is durable only after `fsync`; names (create, rename, remove) are modelled as immediately durable; a power cut drops un-fsynced data (or tears it with `torn_writes=True`). Every `write`, `fsync`, `rename`, `remove` is a crash point.

```python
class Agent:
    def step(self, item):
        fs = self.ctx.fs
        fs.write(f"out/{item}.tmp", report)             # bug: never fsynced...
        fs.rename(f"out/{item}.tmp", f"out/{item}.txt") # ...so a power cut leaves an empty file
        fs.write_atomic("cursor", "...")                # correct recipe: tmp + fsync + rename
```

## Real kills (process mode)

`conform` raises an exception inside your process, so `finally:` blocks run and in-memory caches survive. `conform_process` runs every process life in a fresh interpreter and kills it with `SIGKILL` (`os._exit(137)` where there is no SIGKILL) at the crash point:

```python
from crashresume import conform_process
report = conform_process("myagent.agent:make_agent", ["a", "b", "c"], crash_before=True)
```

Rules: inputs are JSON, keys are `str`/`int`, durable state goes in `ctx.fs` (a real directory in this mode; `ctx.store` raises because it cannot survive a kill). A bad target or a failing crash-free baseline raises `SetupError` instead of passing. The target is imported and executed - only point it at code you trust. Each scenario costs a few process starts, so keep inputs small.

## Minimal repros and replay

`conform(..., minimize=True)` shrinks the inputs (delta debugging) and the crash list per finding kind:

```
minimal repro for effect_replayed: inputs=['b'] crashes=[after #1] - key 'pay:b' issued 2 times
```

`replay(factory, inputs, finding.crashes)` re-runs exactly that one scenario for a debugger.

## Frameworks, CI, command line

Any framework fits if you route its tools through `ctx.tool(fn, key=...)` (sync or `async`) and rebuild the agent from `ctx.store`/`ctx.fs` or your own run-id-keyed store. `crashresume.adapters` has `from_async` (async `step`) and `from_function` (plain `fn(ctx, item)` loops). `examples/langgraph_resume.py` crash-tests a real LangGraph graph (langgraph 1.2.20): unguarded tools re-fire after a mid-node crash, ledger-guarded tools pass.

```bash
crashresume myagent.agent:make_agent --inputs '["a","b","c"]' --thorough --minimize \
    --junit crashresume-junit.xml --sarif crashresume.sarif     # exit 0 pass, 1 findings, 2 bad setup
crashresume myagent.agent:make_agent --inputs '["a","b"]' --process   # real SIGKILLs
```

Reports are deterministic (no timestamps), so CI diffs stay quiet. `action.yml` is a composite GitHub Action (`uses: calasdan-sketch/crashresume@<ref>` with `target:` and `inputs:`); upload the SARIF with `github/codeql-action/upload-sarif`. Untrusted characters (terminal escapes, bidi and zero-width marks) in payloads, keys and exception text are escaped in every output.

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

Durable-execution runtimes test *their own* recovery; `crashresume` tests *your* agent's. Statements below come from each project's own pages or from running the package (install and size checked 2026-11-07 on Linux/Python 3.13; Temporal and agent-effects rows were not run - docs only).

| | crashresume | [durabl](https://pypi.org/project/durabl/) | [Temporal replay testing](https://keithtenzer.com/temporal/Altering_Space-Time_Continuum_Testing_for_Determinism/) | [ordeal](https://pypi.org/project/ordeal/0.1.5/) | [agent-effects (Rust)](https://docs.rs/crate/agent-effects/latest/source/tests/crash.rs) |
|---|---|---|---|---|---|
| What it is | test harness for any `step()` object | durable runtime (SQLite log) with a chaos suite | determinism check of workflow code against recorded histories | chaos testing / fault injection library | effect runtime with per-crash-point tests |
| Tests your code without adopting a runtime | yes | no | no (Temporal workflows) | yes | no |
| Crash at every effect boundary, automatically | yes | yes, 10 instrumented points in its runtime | no (replays given histories) | intermittent crash on a schedule | yes, per lifecycle point |
| Crash during recovery | yes (`max_crashes`) | yes (`DURING_REPLAY`) | no | not described | not described |
| Named idempotency-violation findings | yes | its own runtime's invariants | non-determinism errors | user-written properties | at-most-once assertions |
| Python, zero runtime deps | yes | Python 3.11+, needs pydantic | SDK required | Python; needs hypothesis, coverage, pytest (4 MB + 4 MB installed) | Rust |
| Torn writes / fsync model | yes (`ctx.fs`) | no | no | disk-full and truncated-output faults; no fsync model found in its source | no |
| Real `SIGKILL` of your code | yes (`conform_process`) | no | no | kills Compose services, not your function | n/a |
| Minimal repro, JUnit/SARIF | yes | no | no | own JSON schema | no |

Why it matters: crashes between a side effect and its record repeat the effect - one write-up reproduced a duplicate charge in 3 of 3 trials on Restate ([source](https://nerdleveltech.com/restate-durable-agent-duplicate-tool-calls-tested)), and LangGraph re-runs an interrupted node from the top, so tools that already ran fire again ([source](https://blog.raed.dev/posts/langgraph-hitl)). `crashresume` lets you catch that class of bug in a unit test.

## Benchmark

`python benchmarks/run.py` regenerates [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md) and `RESULTS.json`; CI fails if the committed numbers drift. The corpus (`benchmarks/corpus.py`) holds 8 correct and 16 broken agents, each isolating one bug class (replay, eager checkpoint, lost cursor, one-shot recovery, partial batch, payload drift, keyless double send, swallowed crash, double count, unsynced artifact, rename without fsync, torn cursor, torn journal tail, stale lock, ...). Same oracle for every row; only crash selection and fault model differ.

| strategy | recall | precision | crash points (24 agents) | missed |
|---|---|---|---|---|
| random kill, 5 runs/agent (chaos-style, 200 seeds) | 0.50 | 1.00 | 5 per agent | probabilistic |
| 0.2.0-equivalent (exhaustive, after each effect, files always durable) | 0.50 | 1.00 | 78 | 8 of 16 |
| 0.3.0 `conform()` default | 0.69 | 1.00 | 147 | 5 of 16 |
| 0.3.0 `conform(thorough=True, check_payloads=True)` | 1.00 | 1.00 | 4,726 | none |

Process mode on three agents that keep state in process memory (the simulator keeps it too, so default simulation passes them): `conform_process` flags both broken ones (`effect_replayed`) and passes the correct one; see `RESULTS.md`.

Caveats: the corpus was written together with the tool, so 1.00 is a regression guard, not a field accuracy claim. Building it was still informative: crashresume found a real bug in one of my own "correct" agents (a journal that appended after a torn tail without repairing it), which is now fixed in the corpus. The random-kill row is my own implementation of the chaos-monkey idea, not a third-party tool. Thorough mode costs about 32x the default's scenarios.

## Honest limits

- **Simulation vs real kills.** `conform` simulates death with `InjectedCrash` (a `BaseException`) inside one interpreter, so `finally` blocks and in-process caches still run or survive; use `conform_process` for real kills. Neither drops network connections or models clock jumps or disk-full. `ctx.store` writes are durable immediately (use `ctx.fs` for fsync-aware state), and anything `copy.deepcopy` can copy (including list iterators) survives.
- **File model.** `ctx.fs` assumes names are durable immediately (like POSIX with directory fsync) and tears at half the unsynced tail; real file systems reorder and tear in more ways. Process mode cannot lose page cache (a SIGKILL does not), so torn writes exist only in simulation.
- A subject that converts the injected crash into another exception (`except BaseException: raise X`) is reported as `subject_error`, not `crash_swallowed`.
- **Only what goes through `ctx.effect` is observed.** Subjects that record no effects pass trivially (with a warning). Effects without `key=` cannot be checked for replay (warning only).
- **Deterministic subjects work best.** Crash points come from the baseline run; a subject that issues different effects each run gets "never reached" warnings.
- **Cost grows with depth.** For N effects and an agent that resumes where it left off, the default runs N scenarios; `max_crashes=2` runs N + N(N+1)/2 (65 for N=10); `crash_before=True` roughly quadruples that (240 for N=10); `max_crashes=3` gives 285, or 2000 with `crash_before`. Each scenario replays all inputs. `max_scenarios` (default 10,000) caps both runtime and the pending-scenario queue.
- `step()` is synchronous; `from_async` wraps async agents (it runs each step to completion, so it does not interleave concurrent tasks).
- Process mode is covered by the test-suite on Linux; the Windows path (`os._exit(137)`) runs in CI but has had less use.

## License

MIT. See `LICENSE`.
