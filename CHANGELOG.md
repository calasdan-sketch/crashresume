# Changelog

## 0.3.0 (unreleased)

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
