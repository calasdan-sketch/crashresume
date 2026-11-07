# Round 4: the stranger's first hour

Scope: what someone gets from `pip install crashresume`, not what the repo can do. Rounds 1-3 are not repeated.

**Honesty about method.** The brief asked for independent reviewers. This session was itself a sub-agent with no way to start further agents, so one agent played every role below. The commands are real and were run; the "independence" is not. Nothing here was run on Windows, on Python 3.9 or 3.10, or against a live registry upload.

## Baseline (before any change)

| What | Result |
|---|---|
| `pytest` from a plain checkout | 4 collection errors (`No module named crashresume`) unless installed first |
| `PYTHONPATH=src pytest` | 136 passed, 1 skipped, 100 s |
| `python benchmarks/run.py` | 11.6 s; committed `RESULTS.json` reproduced exactly |
| `ruff check .` | clean; `mypy src` (not configured): 12 errors, all internal variable reuse |

After: 144 passed, 1 skipped; benchmark byte-identical (no behaviour change to detection); ruff clean; mypy 10 internal errors.

## Specialists and findings

| Specialist | Finding | Command that proved it | Status |
|---|---|---|---|
| First-time user (wheel in fresh venv, README only) | README says "four broken" agents; selftest has five. Selftest printed 40+ lines incl. confusing "never reached" warnings. | `crashresume --selftest` | Fixed (README, concise selftest) |
| First-time user | `pytest` in a checkout fails to import the package | `python3 -m pytest -q` | Fixed (`pythonpath = ["src"]`) |
| First-time user | README promises features absent from the registry's 0.1.1 | `pip index versions crashresume` -> 0.1.1, 0.1.0; `pip install crashresume==0.1.1` then `--selftest` | README now says "needs 0.3.0 or newer"; release is Dan's step |
| Release engineer | sdist lacked CHANGELOG, benchmarks, examples, `action.yml` | `tar tzf dist/*.tar.gz` | Fixed (`MANIFEST.in`) |
| Release engineer | Few classifiers, no Source/Changelog URL | wheel `METADATA` | Fixed |
| Release engineer | Wheel: LICENSE, README, py.typed, entry points present; no tests, secrets, personal paths; `twine check --strict` passes; zero dependencies and imports work in a clean venv | `unzip -l`, `twine check --strict`, clean-venv `crashresume`, `python -m crashresume`, pytest fixture | OK |
| Supply-chain reviewer | No runtime dependencies. Secrets/path grep of tracked files clean. GitHub Actions in CI are pinned by tag (`@v4`, `@v5`), not commit SHA | `grep -rIniE` over `git ls-files` | Tags not changed (SHAs cannot be checked offline); listed as NOT-DONE |
| Hostile-input fuzzer | Report-file write to a bad path: raw traceback | `crashresume ... --json /proc/nope/x.json` | Fixed (exit 2, plain line, atomic write) |
| Hostile-input fuzzer | Agent calling `sys.exit(3)` escapes `conform`; CLI exits 3 silently | fuzz script (`SystemExit`) | CLI fixed (exit 2 + reason); library still propagates by design |
| Hostile-input fuzzer | Unhashable key, recursive payload, raising `__eq__`, uncopyable store, NUL/`..`/absolute fs paths, bad option values | fuzz script, 17 cases | all give clear errors or findings, no crash |
| Docs/DX writer | Raw `JSONDecodeError` / `AttributeError` text for typos | CLI with `--inputs notjson`, `qs.py:nope` | Fixed (plain messages) |
| Performance engineer | 500 unreconciled inputs: 125,250 findings, 5.7 s, a 125k-line printout | fuzz script | Fixed (print cap 100; data formats keep all). `sample=5` on 5,000 inputs: 0.4 s |
| Performance engineer | Slowest test is 44 s (`test_process_mode_real_filesystem_two_crashes`), suite 74 s | `pytest --durations=5` | NOT-DONE |
| CI/CD integrator | `action.yml` passes inputs via env vars (no shell injection); CLI exit codes 0/1/2 documented | read `action.yml`, ran CLI | OK. An unwritable report file was a traceback (exit 1); now a plain line and exit 2 |
| Windows reviewer (static only) | `print(report)` crashes on cp1252 consoles | `PYTHONIOENCODING=cp1252` run | Fixed |
| Windows reviewer | Process-mode ledger written with CRLF on Windows | code read | Fixed (`newline="\n"`) |
| Windows reviewer | SARIF URI default split target on the first `:` ("C:\x.py:f" -> "C") | code read | Fixed (last `:`) |
| Windows reviewer | No `shell=True`; `SIGKILL` guarded by `hasattr`; `os.replace` only on subject-owned files; fs names reject reserved devices in `RealFS` | `grep` | OK by reading; **not run on Windows** |
| Competitor's maintainer | durabl 0.1.0 and ordeal 0.3.43 are still the newest versions, so README comparison rows are current | `pip index versions durabl ordeal` | Re-checked versions only; contents not re-read (unverified beyond round 3) |

## Scored ideas (value / differentiation / effort / risk each 1-5; higher value and differentiation are better, higher effort and risk are worse)

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | Plain report-write errors, atomic report files | 4 | 1 | 1 | 1 | DONE |
| 2 | Console-safe output (cp1252, LANG=C) | 4 | 1 | 1 | 1 | DONE |
| 3 | Plain-English setup errors | 4 | 2 | 1 | 1 | DONE |
| 4 | Cap printed findings | 4 | 2 | 1 | 1 | DONE |
| 5 | `sys.exit` in agent = setup error | 3 | 1 | 1 | 1 | DONE |
| 6 | sdist completeness (`MANIFEST.in`) | 3 | 1 | 1 | 1 | DONE |
| 7 | Classifiers, Source/Changelog URLs | 2 | 1 | 1 | 1 | DONE |
| 8 | `pytest` works from a checkout | 3 | 1 | 1 | 1 | DONE |
| 9 | Short, readable `--selftest` | 3 | 1 | 1 | 1 | DONE |
| 10 | Windows newline/SARIF fixes | 3 | 1 | 1 | 1 | DONE |
| 11 | `--timeout` validation | 1 | 1 | 1 | 1 | DONE |
| 12 | README version requirement and honest Windows/3.9 limits | 4 | 1 | 1 | 1 | DONE |
| 13 | Real Windows CI run recorded | 5 | 1 | 2 | 1 | NOT-DONE (needs GitHub) |
| 14 | Pin GitHub Actions to commit SHAs | 3 | 1 | 1 | 1 | NOT-DONE (cannot verify SHAs offline) |
| 15 | Fix remaining 10 mypy errors and add mypy to CI | 2 | 1 | 2 | 1 | NOT-DONE |
| 16 | Speed up the 44 s process-mode test | 2 | 1 | 2 | 2 | NOT-DONE |
| 17 | Trusted publishing (OIDC) release workflow | 4 | 1 | 2 | 3 | NOT-DONE (publishing is Dan's decision) |
| 18 | `--format` option / report to stdout (`--json -`) | 3 | 2 | 2 | 2 | NOT-DONE |
| 19 | Real-kill crash of threads and asyncio interleavings | 4 | 5 | 5 | 4 | NOT-DONE (earlier "not built") |
| 20 | Network-drop / clock-jump / disk-full fault models | 4 | 4 | 5 | 4 | NOT-DONE (earlier "not built") |
| 21 | Parallel scenario execution for big inputs | 3 | 2 | 4 | 3 | NOT-DONE |
| 22 | Author name/e-mail in metadata | 2 | 1 | 1 | 2 | NOT-DONE (Dan's decision; currently the GitHub handle) |

## Could not test

- Windows (any behaviour), Python 3.9 and 3.10 runtime (syntax-parsed for 3.9 only; 3.11 and 3.13 ran the full flow).
- Upload to PyPI, Trusted Publishing, the GitHub Action on a runner.
- The `langgraph` example and its test (skipped: langgraph not installed here).
- Read-only filesystem for real (the sandbox runs as root, so permission bits do not block writes); an unwritable path was simulated with a missing directory instead.
- Competitor contents: only registry versions were re-checked.
