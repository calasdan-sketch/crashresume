# Round 5: failures that are not crashes, and a sturdier test suite

Field specialists this round (new angles; rounds 1-4 used payments, storage, attacker, performance, docs, first-time user, release, supply chain, Windows): a **durable-execution engineer**, a **chaos-testing specialist**, an **agent-framework maintainer**, a **test-methodology expert** (property, differential, golden, mutation), a **competitor's maintainer**, a **release engineer**.

**Honesty about method.** This session could not start further agents, so one agent played every role in sequence. Every finding below names the command that proved it. Nothing was run on Windows or on Python 3.9/3.10 (3.9 syntax was parsed with `ast`; the suite ran on 3.11 and 3.13).

## Author line (mandatory item)

"Calas Automations Inc." is now author, maintainer and copyright holder everywhere it matters. Proof from a freshly built wheel and sdist (`python -m build`, then):

```
unzip -p crashresume-0.3.0-py3-none-any.whl '*/METADATA' | grep -E "^(Author|Maintainer)"
  Author: Calas Automations Inc.
  Maintainer: Calas Automations Inc.
unzip -p ... '*/licenses/LICENSE' | sed -n 3p   ->  Copyright (c) 2026 Calas Automations Inc.
tar xzOf crashresume-0.3.0.tar.gz crashresume-0.3.0/PKG-INFO | grep -E "^(Author|Maintainer)"   (same two lines)
twine check --strict dist/*   ->  both PASSED
crashresume --version   ->  crashresume 0.3.0 (c) Calas Automations Inc.   (from the installed wheel in a clean venv)
```

Also: `crashresume.__author__`, SARIF `tool.driver.organization`, JUnit `tool` property. `grep -rniE "calasdan|dan calas|DC Enterprises"` over the whole tree now only hits real GitHub URLs (package metadata, SARIF `informationUri`, the README's `uses:` example). No e-mail was added. Pinned by `test_author_string_is_pinned_everywhere`, `test_report_formats_name_the_tool_author`, `test_wheel_metadata_names_the_author_when_a_wheel_exists`. Whether the GitHub repository is public could not be checked (GitHub access was off); PUBLISH-CHECKLIST tells Dan what to remove if it is private.

## Baseline (before any change) and after

| What | Before | After |
|---|---|---|
| `pytest` | 145 passed, 36 s idle (109 s under load) | 279 passed, 1 skipped (a wheel-metadata test that needs `dist/`), about 20 s idle; same on 3.11 and 3.13 from the built sdist against the installed wheel |
| `python benchmarks/run.py` | 14.5 s; committed numbers reproduced | all earlier rows byte-identical (24-agent: thorough 16/16, 0 false alarms; directories 2/2); two new tables added |
| `ruff check .` | clean | clean |
| `mypy src` | 10 errors (variable reuse) | 0; now a CI step on Python 3.12 |
| Mutation check (new) | n/a | 29 of 29 deliberate breakages caught by the fast tests (`benchmarks/MUTANTS.md`) |

## Upgrade path from the registry version (0.1.1)

`pip download crashresume==0.1.1 --no-deps` into scratch. 0.1.x writes **no data, config or state files** (no cache, no ledger, no settings), so there is nothing to migrate. The only thing that can break is code written against 0.1.1, so I took the 19 tests of the 0.1.1 tree (git `0ebd0a5`) and ran them against this tree: `PYTHONPATH=src pytest <old tests>` -> **19 passed**. 0.1.1's `Finding(kind, crash_after, detail)` positional form still works (new fields have defaults). Reports from 0.1.1 had no `to_dict`, so no old report file can exist to read. One behaviour change since 0.1.1 stays as documented: a run that tested nothing now fails in the CLI and fixture.

## Specialists and findings

| Specialist | Finding | Command that proved it | Status |
|---|---|---|---|
| Durable-execution engineer | A kill-and-restart test never shows a **lost acknowledgement**: the effect committed, the caller saw a timeout, the code retried and charged twice. Not testable before. | `conform(RetryBlind, ["a","b"], faults=True)` -> `effect_replayed` at "timeout after effect #1"; refusal case correctly passes | Fixed: `faults=True` (timeout after commit, refusal before) |
| Durable-execution engineer | A paused worker that wakes after its replacement finished ("zombie": lost lease, GC pause) repeats effects; a check made before the call cannot stop it (check-then-act) | `conform(Unfenced, ..., stale_worker=True)` -> `stale_worker`; `EpochCheckOnly` (lease epoch in the store) is also flagged | Fixed: `stale_worker=True`, fencing tokens `fence=ctx.generation` -> `FencedOut` |
| Durable-execution engineer | Agents that rely on the **provider** honouring an idempotency key were flagged as double-charging; the only way out was a manual ledger check that is itself racy | `test_dedupe_removes_replays_in_every_mode` (fails without `dedupe=True`) | Fixed: `dedupe=True` on `ctx.effect` / `ctx.tool` |
| Durable-execution engineer | Error escaping `step()` is a process exit, not a power cut: unfsynced files stay | `test_unhandled_fault_is_a_process_exit_not_a_power_cut` | Modelled that way (fault lives never call `power_loss`) |
| Chaos-testing specialist | **Disk full** leaves half a file and the process keeps running; a retried append welds onto the half line. Crash tests restart and repair, so they never see it. | `conform(FaultsJournalRetryWelds, ..., faults=True)` -> `resume_diverged` "disk full during fs.write #1"; `thorough=True` alone passes it | Fixed: `faults=True` also fails each `ctx.fs.write` after half the data (ENOSPC) |
| Chaos-testing specialist | **Differential test SimFS vs RealFS** (400 random op sequences, then 60 seeds in the suite) found two defects: `RealFS.rename` into a new directory raised `FileNotFoundError`; `RealFS.fsync_dir` on an unwritten directory raised, where `SimFS` and the docs treat directories as implicit | `python fsdiff.py` (scratch); now `test_simfs_and_realfs_agree_without_crashes[0..59]` | Fixed |
| Chaos-testing specialist | **Mutation check** (29 deliberate breakages, 4 files). First run showed 0 of 8 killed: my runner let pytest's `pythonpath = ["src"]` put the real tree ahead of the mutated copy (a defect in my tool, not the library). After `-o pythonpath=<copy>`: 4 mutants survived. Two were only message changes (one dropped, one replaced; the replacement still survived and exposed that a key which did not exist in the baseline was untested); the other two were real gaps (a key issued fewer times than the baseline; `thorough` without crash-during-recovery) | `python benchmarks/mutants.py` | 3 tests added; final run 29 of 29 killed (`MUTANTS.md`) |
| Chaos-testing specialist | Flakiness of the threaded scenario | 25 consecutive runs of the stale/fault/dedupe tests | 25 of 25 passed |
| Agent-framework maintainer | The real LangGraph graph (1.2.20) in `examples/` passes crashes, timeouts and refusals once its tools are ledger-guarded, but is flagged under `stale_worker`: two workers on one thread id are not fenced by the checkpointer. Adding `fence=ctx.generation` to the tools passes. | `python examples/langgraph_resume.py` (guarded, unfenced: 8 findings; fenced: PASS) | Example extended; scope of the claim stated in the README |
| Agent-framework maintainer | pytest fixture already forwards `**options`, so the new options work in a unit test | `conform(Agent, inputs, faults=True)` via the fixture signature (read; `test_plugin.py` unchanged and green) | OK |
| Agent-framework maintainer | CI users want the report on a pipe | `crashresume ... --json - 2>/dev/null \| python -c "import json,sys; json.load(sys.stdin)"` -> valid | Fixed: `--json -` / `--junit -` / `--sarif -` |
| Test-methodology expert | Output formats had no byte-exact tests | `tests/golden/*` (text, JSON, JUnit, SARIF, two explain timelines), regenerate with `CRASHRESUME_UPDATE_GOLDEN=1` | Fixed |
| Test-methodology expert | No property-based tests. Hypothesis is not installed here, so a seeded generator builds 16 small agents (guard x checkpoint x retry x file use) checked against a ground-truth oracle, plus determinism, replay, sampling-subset, minimal-repro, `clean()` and `KeyView`-vs-list properties | `pytest tests/test_round5.py -k property` | Fixed |
| Test-methodology expert | While building the stale-worker corpus, the harness flagged **my own "correct" agent**: its checkpoint appended instead of setting, so a second writer duplicated an entry | `conform(StaleProviderDedup, ..., stale_worker=True)` before the fix -> `stale_worker` with `['a','a','b']` | Agent fixed; README says the checkpoint must tolerate a second writer too |
| Competitor's maintainer | ordeal 0.3.43 and durabl 0.1.0 are still the newest on PyPI. Source read (not run): ordeal's `timeout()` raises instead of calling the target (no "committed then timed out" variant found); `stale_state` patches an attribute; no fencing/lease/zombie scenario in either package | `pip download ordeal==0.3.43 durabl==0.1.0 --no-deps`, unzip, `grep -rniE "stale\|zombie\|fenc\|lease"` | README comparison row added (marked as source-read, not run) |
| Release engineer | Fresh wheel and sdist: author lines right, `twine check --strict` passes, sdist now carries `tests/golden/` | `unzip -p`, `tar tzf`, `twine check --strict` | Fixed (`MANIFEST.in`) |
| Performance engineer | The 44 s process-mode test is dominated by interpreter start-up and `import crashresume` (about 180 ms of CPU each), not fsync (7 ms each; removing the ledger fsync changed nothing and was reverted) | `python -X importtime`, a timing loop | Cause recorded. `conform_process(workers=N)` runs scenarios in parallel and gives an identical report (tested), but **the speed-up could not be measured**: on this machine two parallel busy processes took 7.0 s against 2.6 s for one |
| Hostile-input fuzzer | 300 inputs with `faults` + `stale_worker` + `sample=4` | scratch script | 0.06 s, 16 scenarios |
| Hostile-input fuzzer | Terminal escape and bidi characters in effect kinds and keys under the new options | scratch script | escaped (0 raw ESC in output) |
| Hostile-input fuzzer | `fence="x"`, `fence=True` | scratch script | plain `TypeError` ("fence must be an int (pass ctx.generation)") |
| Hostile-input fuzzer | Replacement worker calling `sys.exit` under `stale_worker` | scratch script | propagates from the library by design (the CLI turns it into exit 2); the paused worker is released and no thread is left behind |
| Hostile-input fuzzer | Odd locale and a path with spaces | `LANG=C PYTHONIOENCODING=cp1252 crashresume ... --faults`; `test_module_runs_from_a_path_with_spaces` | OK |

## Scored ideas (value / differentiation / effort / risk each 1-5; higher value and differentiation are better, higher effort and risk are worse)

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | Author line "Calas Automations Inc." everywhere, pinned by tests | 4 | 1 | 1 | 1 | DONE |
| 2 | Timeout after commit (lost acknowledgement) fault | 5 | 4 | 2 | 2 | DONE |
| 3 | Refusal before the effect ran | 3 | 2 | 1 | 1 | DONE |
| 4 | Disk full mid-write (half written, ENOSPC) | 4 | 4 | 3 | 2 | DONE |
| 5 | Stale worker / zombie scenario | 5 | 5 | 4 | 3 | DONE |
| 6 | Fencing tokens (`fence=`, `ctx.generation`, `FencedOut`) | 4 | 5 | 2 | 2 | DONE |
| 7 | `dedupe=True` for resources that honour idempotency keys | 4 | 4 | 2 | 3 | DONE |
| 8 | RealFS parity with SimFS (differential test) | 3 | 2 | 1 | 1 | DONE |
| 9 | Property tests with a seeded generator (no new dependency) | 3 | 1 | 2 | 1 | DONE |
| 10 | Golden files for every output format | 3 | 1 | 1 | 1 | DONE |
| 11 | Mutation sanity check script plus committed result | 3 | 3 | 2 | 1 | DONE |
| 12 | Benchmark corpora for faults and stale workers | 3 | 3 | 2 | 1 | DONE |
| 13 | `--json -` / `--junit -` / `--sarif -` to standard output | 3 | 1 | 1 | 1 | DONE (prior round 4 row 18) |
| 14 | Fix remaining mypy errors, add mypy to CI | 2 | 1 | 2 | 1 | DONE (prior row 15) |
| 15 | Parallel scenario execution (process mode `workers=N`) | 3 | 2 | 3 | 3 | DONE, speed-up unmeasured (prior row 21) |
| 16 | Speed up the 44 s process-mode test | 2 | 1 | 2 | 2 | NOT-DONE: cause found (start-up cost); `workers` is the lever but not measurable here (prior row 16) |
| 17 | `ctx.generation` in process mode | 2 | 2 | 1 | 1 | DONE |
| 18 | Real Windows CI run recorded | 5 | 1 | 2 | 1 | NOT-DONE: needs GitHub (prior row 13) |
| 19 | Pin GitHub Actions to commit SHAs | 3 | 1 | 1 | 1 | NOT-DONE: SHAs cannot be checked offline (prior row 14) |
| 20 | Trusted-publishing (OIDC) release workflow | 4 | 1 | 2 | 3 | NOT-DONE: publishing is Dan's decision (prior row 17) |
| 21 | Exhaustive interleavings of two live workers (not just "wakes after the replacement finished") | 4 | 5 | 5 | 4 | NOT-DONE (prior row 19, partly covered by #5) |
| 22 | Clock-jump fault (`ctx.clock`) | 3 | 3 | 3 | 3 | NOT-DONE (prior row 20; network drop = #2/#3, disk full = #4) |
| 23 | Hypothesis-based strategies when hypothesis is installed | 2 | 1 | 2 | 1 | NOT-DONE: hypothesis not installed here; seeded generator used instead |
| 24 | LangGraph durability modes (`exit` vs `sync`) example | 3 | 3 | 3 | 3 | NOT-DONE: needs real kills (the simulator's exception unwinds through LangGraph's own exit handling); process mode would be required |
| 25 | Import a production trace (OpenTelemetry) as the baseline instead of running the agent | 4 | 5 | 5 | 4 | NOT-DONE |
| 26 | Static lint: retry loops without a key check | 3 | 3 | 4 | 4 | NOT-DONE (false-positive risk) |
| 27 | Fault injection in process mode (real SIGKILL plus faults) | 3 | 3 | 4 | 3 | NOT-DONE: faults are simulation-only, the CLI warns |
| 28 | Author e-mail in metadata | 1 | 1 | 1 | 2 | NOT-DONE: none given; none invented (prior row 22 partly closed by #1) |

## What I could not test

- Windows (any behaviour), Python 3.9 and 3.10 runtime (3.9 syntax parsed only; 3.11 and 3.13 ran the full suite from the built sdist against the installed wheel).
- The speed-up of `workers=N` (shared machine, effectively one busy core). The report equality is tested; the speed claim is not made.
- Upload to PyPI, the GitHub Action on a runner, whether the GitHub repository is public.
- A real network, a real full disk, a real lease service: every fault is simulated. `stale_worker` tests one interleaving (wake after the replacement finished) with one live `ctx.store` dict; it does not prove fencing correct in a real deployment.
- Competitor contents were read, not run.
- The langgraph result describes the example graph as built, with the in-memory checkpointer, not LangGraph in production.
