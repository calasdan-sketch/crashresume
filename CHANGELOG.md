# Changelog

## 0.2.0 (unreleased)
- `crash_before=True`: also crash just before each effect commits. Catches agents that checkpoint "done" before doing the work (`effect_lost`).
- `max_crashes=N`: crash the resume itself at each of its effect boundaries (crash during recovery). Breadth-first, capped by `max_scenarios` (default 10,000) with a warning when truncated.
- `ctx.store`: a harness-owned dict, durable across restarts within a scenario and fresh for each scenario. No more hand-rolled per-`run_id` stores.
- New finding kinds: `effect_diverged` (same idempotency key re-issued with a different payload), `subject_error` (ordinary exception after a crash is reported instead of aborting the run), `crash_swallowed` (subject caught the injected crash with `except BaseException`).
- Warning when an injected crash is never reached (subject issued fewer effects than the baseline).
- `Finding` gains `scenario` and `when` fields (defaults keep positional construction working). `ConformanceReport.to_dict()` / `to_json()` for CI artifacts.
- Hardening: bounded reprs in messages (huge or hostile payloads), depth-bounded emptiness check, state comparison that survives self-referential containers and raising `__eq__`; O(n) key counting.
- pytest `conform` fixture forwards the new options. `--selftest` demonstrates both new modes.
- Behaviour change: a key issued twice in the baseline and twice after resume is no longer flagged (the baseline defines the expected count).

## 0.1.1
- InjectedCrash now derives from BaseException so broad except Exception cannot hide it.
- Fixed substring match that could suppress duplicate-key findings.
- Report.warnings: flags runs that record no effects, and repeated keyless effects.
- Added python -m crashresume, py.typed, project URLs, ruff, Windows CI. Non-ASCII dashes removed from output and docs.
- CLI with no arguments now exits 2.
- README documents the crash model limits.
