"""crashresume CLI: crash-test an agent (``crashresume pkg.mod:factory --inputs '["a","b"]'``) or run the self-test demo."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .core import Context, clean, conform


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
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


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
            report = conform_process(args.target, inputs, timeout=args.timeout, **opts)
        else:
            from ._child import _load
            sys.path.insert(0, os.getcwd())
            report = conform(_load(args.target), inputs, torn_writes=args.torn_writes, minimize=args.minimize,
                             fail_fast=args.fail_fast, **opts)
    except Exception as exc:  # noqa: BLE001 - bad setup must not look like a pass
        print(f"crashresume: cannot run: {type(exc).__name__}: {clean(str(exc))}", file=sys.stderr)
        return 2
    print(report)
    if args.json:
        _write(args.json, report.to_json(indent=2, sort_keys=True) + "\n")
    if args.junit:
        _write(args.junit, report.to_junit(args.target))
    if args.sarif:
        _write(args.sarif, report.to_sarif(args.sarif_uri or args.target.split(":")[0]))
    return 0 if report.passed else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="crashresume", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"crashresume {__version__}")
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
    ap.add_argument("--fail-fast", action="store_true", help="stop at the first finding")
    ap.add_argument("--process", action="store_true", help="really SIGKILL a fresh subprocess per life (inputs must be JSON)")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-process timeout in seconds (--process)")
    ap.add_argument("--json", metavar="PATH", help="write the report as JSON")
    ap.add_argument("--junit", metavar="PATH", help="write a JUnit XML report")
    ap.add_argument("--sarif", metavar="PATH", help="write a SARIF 2.1.0 report")
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
    print(" ", good)
    ok &= good.passed

    replay_stores = _Stores()
    replay = conform(lambda ctx: ReplayAgent(ctx, replay_stores.get(ctx)), inputs)
    print("ReplayAgent (re-fires effects on resume):")
    print(" ", replay)
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
    print(" ", lost)
    ok &= not lost.passed and any(f.kind == "state_lost" for f in lost.findings)

    eager_default = conform(EagerAgent, inputs)
    eager = conform(EagerAgent, inputs, crash_before=True)
    print("EagerAgent (checkpoints before the work) - default mode, then crash_before=True:")
    print(" ", eager_default)
    print(" ", eager)
    ok &= eager_default.passed and any(f.kind == "effect_lost" for f in eager.findings)

    once_single = conform(OneShotRecoveryAgent, inputs)
    once = conform(OneShotRecoveryAgent, inputs, max_crashes=2)
    print("OneShotRecoveryAgent (recovery not crash-safe) - one crash, then max_crashes=2:")
    print(" ", once_single)
    print(" ", once)
    ok &= once_single.passed and any(f.kind == "effect_replayed" for f in once.findings)

    print("\nselftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
