# crashresume

**Prove your agent survives a crash - in a unit test, with zero dependencies.** `crashresume` kills and restarts your agent at every side-effect and file-I/O boundary (after the effect commits, before it commits, mid-write with a torn file, and again during recovery - in simulation or with a real `SIGKILL`), then tells you exactly which crash point double-charged, lost work, or resumed into the wrong state, and shrinks it to a minimal reproduction. Opt-in: a timeout that arrives after the effect already happened, and a paused "zombie" worker that wakes up after its replacement finished.

No server, no database, no framework: the subject is any object with a `step(input)` method. Pure Python 3.9+, no runtime dependencies (pytest only if you use the fixture).

On a labelled corpus of 24 sample agents (16 intentionally broken) the thorough mode finds **16/16 bugs with 0 false alarms**; killing at 5 random points, chaos-monkey style, finds about half (see [Benchmark](#benchmark)).

## Quick start

```bash
pip install crashresume
crashresume --version       # needs 0.3.0 or newer; older releases lack most of what is described below
crashresume --selftest      # one correct agent passes, five broken ones are caught (exit 0 = all as expected)
```

From a checkout instead: `pip install .` (or `pip install -e . pytest` for development; `pytest` also runs without installing).

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
| `stale_worker` | `stale_worker=True` only: a paused worker woke up after its replacement had finished and wrote again (no fencing token) |

Each finding kind has a one-line fix hint in the SARIF output (`help` text of the rule). `str(report)` and the CLI list at most 100 findings and say how many more there are; `report.findings`, `--json`, `--sarif` and `--junit` always hold all of them.

A run that tested nothing (no effects recorded, or no inputs) still returns `passed`, but `report.vacuous` is true; the pytest fixture and the CLI treat that as a failure (`allow_vacuous=True` / `--allow-vacuous` to accept it). Warnings (not failures) flag runs with no effects, a baseline that itself issues a key more than once, keyless effects that repeated, injected crashes that were never reached, and a hit `max_scenarios` cap - a truncated or untested run is never reported as a silent pass.

## Crash modes

| Option | Crash point | Catches |
|---|---|---|
| default | right **after** each effect commits, before your checkpoint | replays (the classic double charge) |
| `crash_before=True` | right **before** each effect commits | checkpointing "done" before doing the work |
| `max_crashes=2` (or more) | also crash the **resume** at each of its own effects | recovery code that is only safe once |
| `torn_writes=True` | after each `ctx.fs.write`, power cut with the un-fsynced tail torn to half | non-atomic checkpoint files, unvalidated journal tails |
| `strict_keyless=True` | repeated or vanished keyless effects become findings | duplicate emails with no idempotency key |
| `thorough=True` | all of the above plus `max_crashes>=2` | the whole menu in one switch |
| `strict_dirs=True` | file *names* (create, rename, remove) are lost until `fs.fsync_dir(dir)`; not part of `thorough` | rename or new file that was fsynced but whose directory was not |
| `sample=N` | test only N evenly spaced crash points per crash kind and level (first and last always included) | keeps huge inputs affordable; the report says how many points were skipped, so a pass is never read as a proof |
| `check_determinism=True` (on in `thorough`) | runs the crash-free baseline twice and warns if crash points or keys differ | crash numbers that do not line up between runs (time, randomness, state not keyed by `ctx.run_id`) |
| `max_scenarios=10_000` | safety cap on scenarios run (breadth-first) | keeps `max_crashes>=3` bounded |

Scenarios are named by `ctx.run_id`: baseline `run`, `run-3` (after crash point 3), `run-b3` (before point 3), `run-t3` (torn write at point 3), `run-3-1` (after 3, then after the resume's first point). A crash point is every `ctx.effect` / `ctx.tool` call and every mutating `ctx.fs` call.

## Failures that are not crashes: timeouts, refusals, full disks, stale workers

Real systems fail in ways a kill-and-restart test never shows. These are opt-in (`faults=True`, `stale_worker=True`, or `--faults`, `--stale-worker`) and are not part of `thorough`.

**Timeouts, refusals and full disks (`faults=True`).** At every `ctx.effect` / `ctx.tool` call the harness runs the scenario twice more: once where the effect *commits* but the caller gets `InjectedTimeout` (the acknowledgement was lost - the classic cause of a double charge after a "retry"), and once where the effect *does not run* and the caller gets `InjectedRefusal`. At every `ctx.fs.write` it runs one more where half the data is stored and the write then fails with `OSError(ENOSPC)` (a retry that appends after the half line welds the two together). All are ordinary `OSError`s, so `except OSError` / `except Exception` in your code catches them. An error that escapes `step()` counts as the process exiting (files already written stay; there is no power cut), followed by a restart; an error the subject handles is judged on the finished run.

```python
def charge(self, job):
    for _ in range(3):
        if f"charge:{job}" in self.ctx.effects.keys():   # did the earlier try go through after all?
            return
        try:
            self.ctx.effect("charge", job, key=f"charge:{job}")
            return
        except OSError:
            continue                                      # without the check above this is a double charge
```

**Stale workers (`stale_worker=True`).** The first worker pauses just before effect *k* (a long GC pause, a lost lease, a frozen VM). A replacement worker starts, runs every input to the end, and only then does the first one wake up and carry on with whatever it had already decided. Both see the same durable `ctx.store` / `ctx.fs`. A check made before the call is not enough (the pause can fall between the check and the call); the resource must reject old writers. In this harness that is a fencing token: `ctx.effect(..., fence=ctx.generation)` (also `ctx.tool(..., fence=...)`). `ctx.generation` is 1 for the first worker and grows with every restart; an effect carrying a token older than the newest one seen raises `FencedOut` and is not recorded. Without a token the second write is reported as `stale_worker`. The other safe design is a resource that honours the idempotency key itself (a payment provider given an idempotency key): say so with `ctx.effect(..., key=..., dedupe=True)` (or `ctx.tool(..., dedupe=True)`), and a second effect with a key the resource already has is ignored - nothing is recorded, nothing is counted as a replay - in every mode, not just here. The rest of the subject must tolerate a second writer too (a checkpoint that appends twice is reported as `stale_worker`). Claim `dedupe=True` only for a resource that really does it; the harness cannot check, and it removes the replay check for that call. In process mode a deduplicated `ctx.tool` call returns `None` for a key first used in an earlier process life.

```python
self.ctx.effect("charge", job, key=f"charge:{job}", fence=self.ctx.generation)   # stale writer is rejected
```

Cost: `faults=True` adds two scenarios per effect (timeout, refusal) and one per `ctx.fs.write` (full disk); `stale_worker=True` adds one per effect. Process mode ignores both (it prints a warning).

## Files: fsync, rename, torn writes

`ctx.fs` is an in-memory file system that survives restarts *minus whatever was never fsynced*. Data is durable only after `fsync`; by default names (create, rename, remove) are modelled as immediately durable; a power cut drops un-fsynced data (or tears it with `torn_writes=True`). With `strict_dirs=True` names are durable only after `ctx.fs.fsync_dir(dir)` (the ext4/XFS-style rule: a file you created and fsynced can still vanish, and a rename can revert, until its directory is fsynced). Every `write`, `fsync`, `fsync_dir`, `rename`, `remove` is a crash point; `write_atomic` is the full correct recipe (temp file, fsync, rename and, under `strict_dirs`, directory fsync).

```python
class Agent:
    def step(self, item):
        fs = self.ctx.fs
        fs.write(f"out/{item}.tmp", report)             # bug: never fsynced...
        fs.rename(f"out/{item}.tmp", f"out/{item}.txt") # ...so a power cut leaves an empty file
        fs.write_atomic("cursor", "...")                # correct recipe: tmp + fsync + rename (+ fsync_dir under strict_dirs)
```

A step that mutates its input (`job.pop("n")`) is safe: every process life gets its own deep copy of the inputs (inputs that cannot be copied are shared).

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

`replay(factory, inputs, finding.crashes)` re-runs exactly that one scenario for a debugger. `explain(factory, inputs, finding.crashes)` returns a timeline of it (also `crashresume ... --explain`; abridged here, the real output also lists the baseline and every effect across all lives):

```
scenario run-1: crash after point #1
process life 1: killed after point #1
  #1 effect: charge key='charge:a' payload='a'   <-- KILLED right after this committed
  -- process dies here; un-fsynced file data is lost
resume (clean process, no crash)
  #1 effect: charge key='charge:a' payload='a'
  #2 effect: charge key='charge:b' payload='b'
finding [effect_replayed]: key 'charge:a' issued 2 times
```

## Frameworks, CI, command line

Any framework fits if you route its tools through `ctx.tool(fn, key=...)` (sync or `async`) and rebuild the agent from `ctx.store`/`ctx.fs` or your own run-id-keyed store. `crashresume.adapters` has `from_async` (async `step`) and `from_function` (plain `fn(ctx, item)` loops). `examples/langgraph_resume.py` crash-tests a real LangGraph graph (langgraph 1.2.20): unguarded tools re-fire after a mid-node crash; ledger-guarded tools pass crashes, timeouts and refusals but fail `stale_worker` (two workers on one thread: its checkpointer does not fence the paused one); adding `fence=ctx.generation` to each tool passes everything. That is a statement about the graph as built in the example, not about every LangGraph deployment.

```bash
crashresume myagent.agent:make_agent --inputs '["a","b","c"]' --thorough --minimize \
    --junit crashresume-junit.xml --sarif crashresume.sarif     # exit 0 pass, 1 findings, 2 bad setup or unwritable report file
crashresume myagent.agent:make_agent --inputs '["a","b"]' --process   # real SIGKILLs
crashresume myagent.agent:make_agent --inputs '["a","b"]' --faults --stale-worker   # timeouts, refusals, zombie worker
```

`--json -`, `--junit -` or `--sarif -` writes that report to standard output (the text report then goes to standard error). `--workers N` (with `--process`) runs N scenarios at once; the report is identical to a single worker, and only the wall-clock time changes (not measured in the environment this was built in, which gave about one usable core).

Reports are deterministic (no timestamps), so CI diffs stay quiet. `action.yml` is a composite GitHub Action (`uses: calasdan-sketch/crashresume@<ref>` with `target:` and `inputs:`); upload the SARIF with `github/codeql-action/upload-sarif`. Untrusted characters (terminal escapes, bidi and zero-width marks) in payloads, keys and exception text are escaped in every output. Setup mistakes (bad JSON, unknown module, unwritable report path, an agent that calls `sys.exit`) print one plain line on stderr and exit 2, never a traceback; a console that cannot show a character (Windows cp1252, `LANG=C`) gets an escape instead of an error; report files are written to a temp file and renamed, so CI never sees a half-written report.

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

Route real side effects through `ctx.effect(kind, payload, key=...)` (keys must be hashable; strings recommended, and `""` counts as a real key) - that is where crashes are injected and replays counted. `ctx.effects` is the durable log of what already happened, for reconciliation: `key in ctx.effects.keys()` and `ctx.effects.has(key)` are O(1), so reconciling on every step stays cheap.

## How it compares

Durable-execution runtimes test *their own* recovery; `crashresume` tests *your* agent's. Statements below come from each project's own pages or from running the package (package metadata and source of durabl 0.1.0 and ordeal 0.3.43 re-read 2026-11-08, still the newest versions, and crashresume installed and run on Linux/Python 3.13; Temporal and agent-effects rows were not run - docs only; ordeal and durabl were downloaded and read but not run).

| | crashresume | [durabl](https://pypi.org/project/durabl/) | [Temporal replay testing](https://keithtenzer.com/temporal/Altering_Space-Time_Continuum_Testing_for_Determinism/) | [ordeal](https://pypi.org/project/ordeal/0.3.43/) | [agent-effects (Rust)](https://docs.rs/crate/agent-effects/latest/source/tests/crash.rs) |
|---|---|---|---|---|---|
| What it is | test harness for any `step()` object | durable runtime (SQLite log) with a chaos suite | determinism check of workflow code against recorded histories | chaos testing / fault injection library | effect runtime with per-crash-point tests |
| Tests your code without adopting a runtime | yes | no | no (Temporal workflows) | yes | no |
| Crash at every effect boundary, automatically | yes | yes, 10 instrumented points in its runtime | no (replays given histories) | intermittent crash on a schedule | yes, per lifecycle point |
| Crash during recovery | yes (`max_crashes`) | yes (`DURING_REPLAY`) | no | not described | not described |
| Named idempotency-violation findings | yes | its own runtime's invariants | non-determinism errors | user-written properties | at-most-once assertions |
| Python, zero runtime deps | yes (3.9+) | Python 3.11+, needs pydantic | SDK required | Python 3.12+; needs hypothesis, coverage, packaging, pytest | Rust |
| Torn writes / fsync model | yes (`ctx.fs`, incl. directory fsync with `strict_dirs`) | no | no | disk-full and truncated-output faults; no fsync model found in its source | no |
| Real `SIGKILL` of your code | yes (`conform_process`) | no | no | kills Compose services, not your function | n/a |
| Minimal repro, JUnit/SARIF | yes | no | no | own JSON schema | no |
| Timeout after the effect committed; stale writer with fencing tokens | yes (`faults=True`, `stale_worker=True`, `fence=`) | no | no | `timeout()` raises instead of calling the target (source read); `stale_state` patches an attribute; no fencing found | not described |

Why it matters: crashes between a side effect and its record repeat the effect - one write-up reproduced a duplicate charge in 3 of 3 trials on Restate ([source](https://nerdleveltech.com/restate-durable-agent-duplicate-tool-calls-tested)), and LangGraph re-runs an interrupted node from the top, so tools that already ran fire again ([source](https://blog.raed.dev/posts/langgraph-hitl)). `crashresume` lets you catch that class of bug in a unit test.

## Benchmark

`python benchmarks/run.py` regenerates [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md) and `RESULTS.json`; CI fails if the committed numbers drift. The corpus (`benchmarks/corpus.py`) holds 8 correct and 16 broken agents, each isolating one bug class (replay, eager checkpoint, lost cursor, one-shot recovery, partial batch, payload drift, keyless double send, swallowed crash, double count, unsynced artifact, rename without fsync, torn cursor, torn journal tail, stale lock, ...). Same oracle for every row; only crash selection and fault model differ.

| strategy | recall | precision | crash points (24 agents) | missed |
|---|---|---|---|---|
| random kill, 5 runs/agent (chaos-style, 200 seeds) | 0.50 | 1.00 | 5 per agent | probabilistic |
| 0.2.0-equivalent (exhaustive, after each effect, files always durable) | 0.50 | 1.00 | 78 | 8 of 16 |
| 0.3.0 `conform()` default | 0.69 | 1.00 | 147 | 5 of 16 |
| 0.3.0 `conform(thorough=True, check_payloads=True)` | 1.00 | 1.00 | 4,726 | none |

Directory durability, on a separate 4-agent corpus (2 correct; 2 broken because a new name is never made durable): `thorough=True` alone finds 0 of 2 (it assumes names are durable), `thorough=True, strict_dirs=True` finds 2 of 2 with 0 false alarms (2,232 vs 1,992 crash points). It is a separate corpus so the 24-agent numbers stay comparable with 0.2.0/0.3.0.

Failure-injection corpora (separate, so the numbers above stay comparable): 7 agents for `faults=True` (4 broken: a retry that does not ask whether the first try went through, a retry under a fresh key, an ignored error followed by "done", a journal retried after a half-written line) and 5 for `stale_worker=True` (2 broken: no fencing token; an epoch check made before the call). Without the new options `thorough=True` finds 0 of 4 and 0 of 2; with them it finds 4 of 4 and 2 of 2, and flags none of the 6 correct agents. The same caveat applies: written together with the tool, and while writing it the harness caught a flaw in one of my own "correct" stale-worker agents (a checkpoint that appended instead of setting).

`python benchmarks/mutants.py` breaks the library on purpose in 29 ways and checks the fast tests notice; the result is in [`benchmarks/MUTANTS.md`](benchmarks/MUTANTS.md).

Process mode on three agents that keep state in process memory (the simulator keeps it too, so default simulation passes them): `conform_process` flags both broken ones (`effect_replayed`) and passes the correct one; see `RESULTS.md`.

Caveats: the corpus was written together with the tool, so 1.00 is a regression guard, not a field accuracy claim. Building it was still informative: crashresume found a real bug in one of my own "correct" agents (a journal that appended after a torn tail without repairing it), which is now fixed in the corpus. The random-kill row is my own implementation of the chaos-monkey idea, not a third-party tool. Thorough mode costs about 32x the default's scenarios.

## Honest limits

- **Simulation vs real kills.** `conform` simulates death with `InjectedCrash` (a `BaseException`) inside one interpreter, so `finally` blocks and in-process caches still run or survive; use `conform_process` for real kills. Neither drops network connections or models clock jumps or disk-full. `ctx.store` writes are durable immediately (use `ctx.fs` for fsync-aware state), and anything `copy.deepcopy` can copy (including list iterators) survives.
- **File model.** `ctx.fs` assumes names are durable immediately (like POSIX with directory fsync) and tears at half the unsynced tail; real file systems reorder and tear in more ways. Process mode cannot lose page cache (a SIGKILL does not), so torn writes exist only in simulation.
- A subject that converts the injected crash into another exception (`except BaseException: raise X`) is reported as `subject_error`, not `crash_swallowed`.
- **Only what goes through `ctx.effect` is observed.** Subjects that record no effects pass trivially (with a warning). Effects without `key=` cannot be checked for replay (warning only).
- **Deterministic subjects work best.** Crash points come from the baseline run; a subject that issues different effects each run gets "never reached" warnings.
- **Directory model.** `strict_dirs` is one specific stricter rule (names durable per directory on `fsync_dir`); it does not model every reordering a real file system may do, and a cross-directory rename that survives only half can leave the file under both names (the model keeps both). Process mode cannot lose names or data (SIGKILL keeps the page cache), so `strict_dirs` is simulation-only and the CLI says so.
- **Faults and stale workers are two specific models.** `faults=True` fails one call at a time, at `ctx.effect` / `ctx.tool` (timeout, refusal) and `ctx.fs.write` (full disk, half written; other file operations never fail); it does not model slow calls, partial responses, clock jumps or a disk that fills up later. `stale_worker=True` tests one interleaving - the first worker wakes after its replacement has finished - not every interleaving of two live workers; it runs the first worker on a thread, shares `ctx.store` as one live dict (a database, not a snapshot), and treats any exception the woken worker dies of as "stopped". Neither runs in process mode.
- **Threads.** Crash points must be hit on the thread that runs `step()`; an exception on a worker thread cannot stop the process, and is reported as `crash_swallowed` with a hint.
- **Inputs are deep-copied per process life.** Inputs that depend on object identity (a shared client, a mock you assert on) should be built inside the factory instead.
- **`sample=N` is a subset, not a proof.** It exists for inputs with hundreds of steps, where every scenario replays every input (cost is roughly quadratic).
- **Cost grows with depth.** For N effects and an agent that resumes where it left off, the default runs N scenarios; `max_crashes=2` runs N + N(N+1)/2 (65 for N=10); `crash_before=True` roughly quadruples that (240 for N=10); `max_crashes=3` gives 285, or 2000 with `crash_before`. Each scenario replays all inputs. `max_scenarios` (default 10,000) caps both runtime and the pending-scenario queue.
- `step()` is synchronous; `from_async` wraps async agents (it runs each step to completion, so it does not interleave concurrent tasks).
- **Windows.** The CI file lists a Windows job, but nobody has run the round-4 changes on Windows and no passing Windows run is on record; support rests on a code review (path separators, `os.replace`, newline and console encoding, no `shell=True`, no `SIGKILL`). Python 3.9 is only syntax-checked locally (the CI file lists it). Process mode on Windows uses `os._exit(137)` instead of `SIGKILL`.

## License

MIT. Copyright (c) 2026 Calas Automations Inc. See `LICENSE`.
