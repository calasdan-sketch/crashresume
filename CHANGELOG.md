# Changelog

## 0.3.0 (unreleased)

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
