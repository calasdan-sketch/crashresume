"""crashresume CLI: crash-test an agent (``crashresume pkg.mod:factory --inputs '["a","b"]'``) or run the self-test demo."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import __author__, __version__
from .core import Context, clean, conform, explain


class _Store:
    """Stand-in for durable storage shared across process restarts."""

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}


class _Stores:
    """Per-scenario durable stores, isolated by ctx.run_id (see README)."""

    def __init__(self) -> None:
        self.d: Dict[str, _Store] = {}

    def get(self, ctx) -> "_Store":
        return self.d.setdefault(ctx.run_id, _Store())


class GoodAgent:
    """Persists after every input and reconciles against committed effects."""

    def __init__(self, ctx: Context, store: _Store) -> None:
        self.ctx = ctx
        self.store = store
        self.done = set(store.data.get("done", set()))

    def step(self, item: str) -> None:
        key = f"process:{item}"
        if item in self.done or key in self.ctx.effects.keys():
            self.done.add(item)
            self.store.data["done"] = set(self.done)
            return
        self.ctx.effect("process", item, key=key)
        self.done.add(item)
        self.store.data["done"] = set(self.done)

    def state(self):
        return {"done": sorted(self.store.data.get("done", set()))}


class ReplayAgent:
    """Persists state but re-issues effects for everything on resume."""

    def __init__(self, ctx: Context, store: _Store) -> None:
        self.ctx = ctx
        self.store = store

    def step(self, item: str) -> None:
        self.ctx.effect("process", item, key=f"process:{item}")  # always fires
        self.store.data.setdefault("done", set()).add(item)

    def state(self):
        return {"done": sorted(self.store.data.get("done", set()))}


class LostAgent:
    """Loses its checkpoint and wrongly believes the stream is exhausted."""

    def __init__(self, ctx: Context, store: _Store) -> None:
        self.ctx = ctx
        self.store = store

    def step(self, item: str) -> None:
        if self.store.data.get("exhausted"):
            return
        self.ctx.effect("process", item, key=f"process:{item}")
        self.store.data.setdefault("done", []).append(item)

    def state(self):
        return {"done": list(self.store.data.get("done", []))}


class EagerAgent:
    """Checkpoints "done" BEFORE doing the work: safe for crash-after, loses work on crash-before."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.ctx.store.setdefault("done", [])

    def step(self, item: str) -> None:
        if item in self.ctx.store["done"]:
            return
        self.ctx.store["done"].append(item)        # checkpoint first (the bug)
        self.ctx.effect("process", item, key=f"process:{item}")

    def state(self):
        return {"done": sorted(self.ctx.store["done"])}


class OneShotRecoveryAgent:
    """Reconciles against the effect log only on the FIRST restart: survives one
    crash, replays an effect if the recovery run itself crashes."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        st = ctx.store
        st.setdefault("done", [])
        if st.get("started") and not st.get("recovered"):
            st["recovered"] = True                   # flag set before healing finishes
            st["done"] = sorted(set(st["done"]) | {k.split(":", 1)[1] for k in ctx.effects.keys()})
        st["started"] = True

    def step(self, item: str) -> None:
        if item in self.ctx.store["done"]:
            return
        self.ctx.effect("process", item, key=f"process:{item}")
        self.ctx.store["done"].append(item)

    def state(self):
        return {"done": sorted(self.ctx.store["done"])}


def _write(path: str, text: str) -> None:
    """Write a report file ('-' = standard output); a crash halfway never leaves a half-written file behind."""
    if path == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise OSError(exc.errno, exc.strerror, path) from None   # name the file the user asked for, not the temp
        raise


def _plain(exc: BaseException) -> str:
    """One plain-English line for a setup problem (no Python jargon where we can avoid it)."""
    msg = clean(str(exc))
    if isinstance(exc, json.JSONDecodeError):
        return f"the inputs are not valid JSON ({clean(exc.msg)} at character {exc.pos}); write a list such as '[\"a\",\"b\"]'"
    if isinstance(exc, ModuleNotFoundError):
        return (f"{msg}. Run crashresume from the folder that contains your code, install your package, "
                "or pass a file path such as path/to/agent.py:factory")
    if isinstance(exc, FileNotFoundError):
        return f"file not found: {clean(str(exc.filename or msg))}"
    if isinstance(exc, AttributeError) and "has no attribute" in msg:
        return f"{msg}. Check the name after the ':' in the target"
    return f"{type(exc).__name__}: {msg}"


def _setup_error(exc: BaseException) -> int:
    print(f"crashresume: cannot run: {_plain(exc)}", file=sys.stderr)
    return 2


def _run_target(args: argparse.Namespace) -> int:
    """Crash-test ``args.target``. Exit 0 pass, 1 findings, 2 bad setup (fails closed)."""
    try:
        if args.inputs_file:
            with open(args.inputs_file, "r", encoding="utf-8") as fh:
                inputs = json.load(fh)
        else:
            inputs = json.loads(args.inputs) if args.inputs is not None else None
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("--inputs / --inputs-file must be a non-empty JSON list")
        opts: Dict[str, Any] = dict(
            crash_before=args.crash_before, max_crashes=args.max_crashes, max_scenarios=args.max_scenarios or None,
            strict_keyless=args.strict_keyless, thorough=args.thorough, check_payloads=True if args.check_payloads else None,
        )
        if args.process:
            from .process import conform_process
            ignored = [f for f, on in (("--minimize", args.minimize), ("--fail-fast", args.fail_fast),
                                       ("--torn-writes", args.torn_writes), ("--strict-dirs", args.strict_dirs),
                                       ("--explain", args.explain), ("--check-determinism", args.check_determinism),
                                       ("--faults", args.faults), ("--stale-worker", args.stale_worker)) if on]
            if ignored:
                print(f"crashresume: warning: {', '.join(ignored)} not supported with --process; ignored", file=sys.stderr)
            report = conform_process(args.target, inputs, timeout=args.timeout, sample=args.sample,
                                     workers=args.workers, **opts)
        else:
            from ._child import _load
            if args.workers != 1:
                print("crashresume: warning: --workers only applies with --process; ignored", file=sys.stderr)
            sys.path.insert(0, os.getcwd())
            factory = _load(args.target)
            report = conform(factory, inputs, torn_writes=args.torn_writes, minimize=args.minimize,
                             fail_fast=args.fail_fast, strict_dirs=args.strict_dirs, sample=args.sample,
                             check_determinism=args.check_determinism, faults=args.faults,
                             stale_worker=args.stale_worker, **opts)
    except KeyboardInterrupt:
        print("crashresume: interrupted", file=sys.stderr)
        return 130
    except SystemExit as exc:  # the subject (or its module) called sys.exit() while being crash-tested
        print(f"crashresume: cannot run: the agent called sys.exit({clean(str(exc.code))}) while being tested; "
              "an agent under test must not exit the process", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - bad setup must not look like a pass
        return _setup_error(exc)
    to_stdout = [n for n, v in (("--json", args.json), ("--junit", args.junit), ("--sarif", args.sarif)) if v == "-"]
    if len(to_stdout) > 1:
        print(f"crashresume: cannot run: only one of {', '.join(to_stdout)} can write to '-' (standard output)",
              file=sys.stderr)
        return 2
    human = sys.stderr if to_stdout else sys.stdout   # keep standard output clean when it carries a report
    print(report, file=human)
    if args.explain and not args.process:
        seen: set = set()
        for f in report.findings:
            if f.crashes and f.crashes not in seen and len(seen) < 3:
                seen.add(f.crashes)
                try:
                    print("\n" + explain(factory, inputs, f.crashes, strict_keyless=args.strict_keyless or args.thorough,
                                         strict_dirs=args.strict_dirs,
                                         check_payloads=True if args.check_payloads else None), file=human)
                except Exception as exc:  # noqa: BLE001 - explanation is best effort
                    print(f"crashresume: could not explain {f.scenario}: {type(exc).__name__}: {clean(str(exc))}",
                          file=sys.stderr)
    try:
        if args.json:
            _write(args.json, report.to_json(indent=2, sort_keys=True) + "\n")
        if args.junit:
            _write(args.junit, report.to_junit(args.target))
        if args.sarif:
            _write(args.sarif, report.to_sarif(args.sarif_uri or args.target.rpartition(":")[0]))
    except OSError as exc:  # a report the CI job expects but cannot be written must not look like success
        print(f"crashresume: cannot write report file: {_plain(exc)}", file=sys.stderr)
        return 2
    if report.passed and report.vacuous and not args.allow_vacuous:
        print("crashresume: nothing was tested (no crash scenario ran); failing closed. Route side effects through "
              "ctx.effect / ctx.tool / ctx.fs, or pass --allow-vacuous.", file=sys.stderr)
        return 2
    return 0 if report.passed else 1


def _brief(report) -> str:
    """The self-test demo output: headline plus the first few findings (the full list is in ``str(report)``)."""
    lines = str(report).splitlines()
    head = [ln for ln in lines if not ln.lstrip().startswith("warning:")]
    shown = head[:4]
    more = len(report.findings) - (len(shown) - 1)
    out = "\n".join("  " + ln.strip() if i == 0 else "  " + ln.strip() for i, ln in enumerate(shown))
    if report.findings and more > 0:
        out += f"\n  ... and {more} more"
    return out


def _safe_streams() -> None:
    """Never crash on a console that cannot show a character (Windows cp1252, LANG=C): show an escape instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _safe_streams()
    ap = argparse.ArgumentParser(prog="crashresume", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"crashresume {__version__} (c) {__author__}")
    ap.add_argument("--selftest", action="store_true", help="run the built-in good/bad agent demo")
    ap.add_argument("target", nargs="?", help="agent factory as 'package.module:factory' or 'path/to/file.py:factory'")
    ap.add_argument("--inputs", help="JSON list of inputs fed to step(), e.g. '[\"a\",\"b\"]'")
    ap.add_argument("--inputs-file", help="file holding the JSON list of inputs")
    ap.add_argument("--crash-before", action="store_true", help="also crash just before each effect")
    ap.add_argument("--max-crashes", type=int, default=1, help="crashes per scenario (2 = crash during recovery)")
    ap.add_argument("--max-scenarios", type=int, default=10_000, help="scenario cap (0 = unlimited)")
    ap.add_argument("--thorough", action="store_true", help="crash-before + 2 crashes + torn writes + strict keyless")
    ap.add_argument("--torn-writes", action="store_true", help="tear un-fsynced ctx.fs writes at each write")
    ap.add_argument("--strict-keyless", action="store_true", help="repeated keyless effects are findings")
    ap.add_argument("--check-payloads", action="store_true", help="flag re-issued keys whose payload changed")
    ap.add_argument("--minimize", action="store_true", help="shrink each finding to a minimal reproduction")
    ap.add_argument("--strict-dirs", action="store_true", help="file names are lost until ctx.fs.fsync_dir() (stricter fs model)")
    ap.add_argument("--sample", type=int, metavar="N", help="test only N evenly spaced crash points per kind/level")
    ap.add_argument("--check-determinism", action="store_true", help="run the crash-free baseline twice and warn if it differs")
    ap.add_argument("--faults", action="store_true",
                    help="also inject a timeout after the effect committed, and a refusal before it ran, at every effect")
    ap.add_argument("--stale-worker", action="store_true",
                    help="also wake a paused first worker after its replacement finished (needs fencing tokens)")
    ap.add_argument("--explain", action="store_true", help="print a step-by-step timeline for the first failing scenarios")
    ap.add_argument("--allow-vacuous", action="store_true", help="exit 0 even when no crash scenario ran")
    ap.add_argument("--fail-fast", action="store_true", help="stop at the first finding")
    ap.add_argument("--process", action="store_true", help="really SIGKILL a fresh subprocess per life (inputs must be JSON)")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-process timeout in seconds (--process)")
    ap.add_argument("--workers", type=int, default=1, metavar="N", help="run N scenarios at once (--process only)")
    ap.add_argument("--json", metavar="PATH",
                    help="write the report as JSON ('-' = standard output, text report to standard error)")
    ap.add_argument("--junit", metavar="PATH", help="write a JUnit XML report ('-' = standard output)")
    ap.add_argument("--sarif", metavar="PATH", help="write a SARIF 2.1.0 report ('-' = standard output)")
    ap.add_argument("--sarif-uri", help="file shown in SARIF locations (default: the target module)")
    args = ap.parse_args(argv)
    if args.target:
        return _run_target(args)
    if not args.selftest:
        ap.print_help()
        return 2
    inputs = ["a", "b", "c"]
    ok = True

    good_stores = _Stores()
    good = conform(lambda ctx: GoodAgent(ctx, good_stores.get(ctx)), inputs)
    print("GoodAgent (persist + reconcile against committed effects):")
    print(_brief(good))
    ok &= good.passed

    replay_stores = _Stores()
    replay = conform(lambda ctx: ReplayAgent(ctx, replay_stores.get(ctx)), inputs)
    print("ReplayAgent (re-fires effects on resume):")
    print(_brief(replay))
    ok &= not replay.passed and any(f.kind == "effect_replayed" for f in replay.findings)

    lost_stores = _Stores()

    def make_lost(ctx):
        store = lost_stores.get(ctx)
        if ctx.run_id != "run":  # checkpoint lost in the crash
            store.data["done"] = []
            store.data["exhausted"] = True
        return LostAgent(ctx, store)

    lost = conform(make_lost, inputs)
    print("LostAgent (checkpoint lost, wrongly believes stream is done):")
    print(_brief(lost))
    ok &= not lost.passed and any(f.kind == "state_lost" for f in lost.findings)

    eager_default = conform(EagerAgent, inputs)
    eager = conform(EagerAgent, inputs, crash_before=True)
    print("EagerAgent (checkpoints before the work) - default mode, then crash_before=True:")
    print(_brief(eager_default))
    print(_brief(eager))
    ok &= eager_default.passed and any(f.kind == "effect_lost" for f in eager.findings)

    once_single = conform(OneShotRecoveryAgent, inputs)
    once = conform(OneShotRecoveryAgent, inputs, max_crashes=2)
    print("OneShotRecoveryAgent (recovery not crash-safe) - one crash, then max_crashes=2:")
    print(_brief(once_single))
    print(_brief(once))
    ok &= once_single.passed and any(f.kind == "effect_replayed" for f in once.findings)

    print("\nselftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
