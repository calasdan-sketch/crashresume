# Changelog

## 0.3.0 (unreleased)

Round 5 (second field panel: durable-execution engineer, chaos-testing specialist, agent-framework maintainer; test-method upgrade)

Author
- The author, maintainer and copyright holder is now "Calas Automations Inc." (`pyproject.toml`, `LICENSE`, `crashresume.__author__`, `crashresume --version`, SARIF `tool.driver.organization`, JUnit `tool` property). Pinned by a test.

Added
- `faults=True` / `--faults`: at every `ctx.effect` / `ctx.tool` the effect either commits and the caller gets `InjectedTimeout` (lost acknowledgement), or never runs and the caller gets `InjectedRefusal`; at every `ctx.fs.write` the disk fills after half the data (`OSError(ENOSPC)`). Both exceptions are `OSError`s. An error that escapes `step()` counts as the process exiting (no power cut), then a restart; a handled error is judged as the finished run. Not part of `thorough`.
- `stale_worker=True` / `--stale-worker`: the first worker pauses before each effect, its replacement runs every input, then the first worker wakes. New finding `stale_worker`. `ctx.effect(..., fence=ctx.generation)` and `ctx.tool(..., fence=...)` give a fencing token: a token older than the newest seen raises `FencedOut` and records nothing. `ctx.generation` is 1 for the first worker and grows with each restart. New exports: `InjectedTimeout`, `InjectedRefusal`, `FencedOut`.
- `conform_process(workers=N)` / `--workers N`: N scenarios at once; the report is identical to a single worker. Speed-up not measured (the build machine had about one usable core).
- `dedupe=True` on `ctx.effect` / `ctx.tool`: the resource honours the idempotency key, so a second effect with a key it already has is ignored and never counted as a replay (claim it only for a resource that really does that).
- `ctx.generation` is also set in process mode. `examples/langgraph_resume.py` now also runs the stale-worker test on the real LangGraph graph.
- `--json -`, `--junit -`, `--sarif -` write the report to standard output (the text report then goes to standard error).
- Benchmark: two small corpora for the new options (`faults=True`: 7 agents, 4 broken, found 0 of 4 before and 4 of 4 with it; `stale_worker=True`: 5 agents, 2 broken, 0 of 2 before and 2 of 2 with it; no false alarms). `benchmarks/mutants.py` breaks the library 29 ways on purpose and checks the tests notice (`benchmarks/MUTANTS.md`).
- Tests: seeded property tests (generated agents against a ground-truth oracle, determinism, replay, sampling subset, minimal repro, `clean`, `KeyView` against a plain list), a SimFS-versus-RealFS differential test, and golden files for the text, JSON, JUnit, SARIF and explain outputs.

Fixed
- `RealFS.rename` into a directory that did not exist yet failed (`SimFS` and the docs say directories are implicit); `RealFS.fsync_dir` on a directory nothing was written to failed where `SimFS` accepted it. Found by the differential test.
- 11 internal type errors fixed; `mypy src` is clean and runs in CI (Python 3.12).

Round 4 (the stranger's first hour: clean-venv install of the built wheel, packaging, hostile input, consoles, Windows review)

Fixed
- CLI: `--json/--junit/--sarif` to an unwritable path ended in a traceback; it is now one plain line, exit 2. Report files are written to a temp file and renamed (no half-written report).
- CLI: a console that cannot encode a payload character (Windows cp1252, `LANG=C`) crashed `print(report)` with `UnicodeEncodeError`; output now falls back to escapes.
- CLI: an agent calling `sys.exit()` made the CLI exit with the agent's code and no message; now exit 2 with an explanation. Ctrl-C exits 130 quietly.
- CLI: plain-English setup errors (invalid JSON, missing module, missing file, wrong factory name); `--timeout 0` or negative is rejected.
- `--sarif` default location used `C` as the file for Windows targets like `C:\x\a.py:make` (split on the first colon); it now splits on the last.
- Process mode wrote the effect ledger with CRLF on Windows; it now always writes `\n`.
- A run with hundreds of unreconciled effects produced 100,000+ finding lines; `str(report)` and the CLI show the first 100 and a count (data formats keep everything). `ConformanceReport.MAX_SHOWN` sets the cap.
- `--selftest` prints a few findings per demo agent instead of every one, without the confusing warnings.

Packaging
- sdist now includes CHANGELOG, benchmarks, examples and `action.yml` (`MANIFEST.in`); classifiers for Python 3.9-3.13, Beta, Console, Developers; `Source` and `Changelog` URLs; `pytest` runs from a plain checkout (`pythonpath = ["src"]`).
- Registry check (read-only): PyPI holds 0.1.1 and 0.1.0, so 0.3.0 is the next release.

Round 3 (second expert panel: payments engineer, storage/file-system engineer, attacker, performance engineer, docs/DX lead, competitor engineer)

Added
- `strict_dirs=True` / `--strict-dirs` and `SimFS(dir_durability=True)`: file names are lost on a power cut until `ctx.fs.fsync_dir(dir)`. `fsync_dir` is a crash point in every mode (`RealFS` too); `write_atomic` includes it under `strict_dirs`. Not part of `thorough` (it would flag code that is fine under the default model).
- `explain(factory, inputs, finding.crashes)` and `--explain`: step-by-step timeline of one scenario (effects with key and payload, file operations, the kill point, the resume, the verdict). Output is escaped.
- `sample=N` / `--sample N`: test N evenly spaced crash points per kind and level for big inputs; the report warns how many points were skipped.
- `check_determinism=True` / `--check-determinism` (on in `thorough`): second crash-free baseline run; warns when crash points or keys differ.
- `report.vacuous`; the pytest fixture fails a run that tested nothing (`allow_vacuous=True` to accept), and the CLI exits 2 (`--allow-vacuous`).
- `ctx.effects.has(key)`, `ctx.effects.count(key)`; `keys()` now returns a list-like `KeyView` (`KeyView` exported).
- SARIF rules carry a fix hint (`help`). The CLI warns when `--process` ignores `--minimize`, `--fail-fast`, `--torn-writes`, `--strict-dirs`, `--explain`, `--check-determinism`. `conform_process(sample=...)`.
- Benchmark: 4-agent directory-durability corpus (`thorough` 0/2, `thorough + strict_dirs` 2/2, no false alarms). The 24-agent numbers are unchanged.

Fixed
- A `step()` that mutated its input (`job.pop(...)`) poisoned every later scenario and produced false findings; each process life now gets a deep copy of the inputs (uncopyable inputs are shared, as before).
- NaN in a payload or in `state()` was reported as `effect_diverged` / `resume_diverged` (NaN != NaN). Builtin values whose reprs match are now equal.
- `ctx.effects.keys()` (the README's reconcile idiom) copied the whole list on every call, making a run cubic in the number of inputs; it is now O(1) per lookup.
- Process mode: a ledger torn by a timeout kill mid-append raised a raw `JSONDecodeError`; a torn final line is now dropped and repaired, any other corruption raises `SetupError`.
- `fs` paths containing NUL are rejected with `ValueError` in both file systems; on Windows `RealFS` also rejects reserved device names, `:` (alternate data streams), trailing dot/space and `<>"|?*`.
- A factory whose object has no `step()` now raises a clear `TypeError`; effects issued on a worker thread are reported as `crash_swallowed` with an explanation instead of a bare traceback-and-mystery.

Changed (behaviour)
- The pytest `conform` fixture and the CLI now fail a vacuous run (nothing was tested) instead of passing with a warning. Library `conform()` still returns normally; check `report.vacuous`.

Earlier in 0.3.0 (rounds 1-2)

Added
- `ctx.fs` (`SimFS`): fsync-aware in-memory file system; write/fsync/rename/remove are crash points; un-fsynced data is lost on a crash. `torn_writes=True` adds torn-write scenarios.
- `conform_process` / `--process`: every process life is a fresh interpreter killed with SIGKILL (finds bugs that `finally:` blocks and in-memory caches hide in simulation). `RealFS` gives subjects the same `ctx.fs` API on a real directory.
- `minimize=True` (delta-debugging of inputs and crash list), `replay()`, `fail_fast=True`; `Finding.crashes`, `Finding.point`, `report.repros`, `report.scenarios`.
- `strict_keyless=True` and `thorough=True`.
- `ctx.tool(fn, key=...)` wraps real tools (sync or async); `crashresume.adapters.from_async` / `from_function`; LangGraph example.
- JUnit XML and SARIF 2.1.0 output (`report.to_junit()`, `report.to_sarif()`), CLI `crashresume pkg.mod:factory --inputs ...` with `--json/--junit/--sarif`, exit codes 0/1/2, composite GitHub Action (`action.yml`).
- `benchmarks/`: labelled corpus of 24 agents, precision/recall against random-kill and 0.2.0-equivalent baselines; CI checks the committed numbers.
- New finding kind `subject_hang` (process mode).

Changed
- Report text says `crash <when> <point> #<n>` (e.g. `fs.write #4`); effect crashes read exactly as before.
- Messages, keys and payloads are escaped for terminal control, bidi and zero-width characters.
- Crash points now include `ctx.fs` mutations; subjects that do not use `ctx.fs` get identical scenarios and counts to 0.2.0.

## 0.2.0
Crash-before, crash-during-recovery, `ctx.store`, panel-review fixes.
